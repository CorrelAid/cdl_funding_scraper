import dlt
import modal.mount
from funding_crawler.spider import FundingSpider
from funding_crawler.dlt_utils.helpers import create_pipeline_runner, cfg_provider
from funding_crawler.helpers import gen_query, pydantic_to_polars_schema, get_hits_count
from scrapy_settings import scrapy_settings

# from funding_crawler.helpers import get_hits_count
from funding_crawler.models import FundingProgramSchema
import polars as pl
import boto3
from markdownify import markdownify as md
import zipfile
from sqlalchemy import create_engine
import os
import modal

license_content = """
Inhalte von foerderdatenbank.de sind individuell lizensiert durch das Bundesministerium für Wirtschaft und Klimaschutz unter CC BY-ND 3.0 DE. 
Der hier zur Verfügung gestellte Datensatz gibt die Informationen zu jedem einzelnen Förderprogramms in einem maschinenlesbaren Format wider. 
Lizenzinformationen zu jedem einzenlnen Förderprogramm inkl. URL können der Datensatz-Spalte license_info sowie der Datei LICENSE-DATA entnommen werden.
"""

image = (
    modal.Image.debian_slim()
    .add_local_file(
        local_path="requirements.txt", remote_path="/root/requirements.txt", copy=True
    )
    .run_commands(
        "pip install uv && uv pip install --system -r /root/requirements.txt",
        "echo 'done'",
    )
    .add_local_file(local_path="dlt_config.toml", remote_path="/root/dlt_config.toml")
    .add_local_file(
        local_path="scrapy_settings.py", remote_path="/root/scrapy_settings.py"
    )
    .add_local_python_source("funding_crawler")
)


app = modal.App(name="cdl_awo_funding_crawler", image=image)

dataset_name = "foerderdatenbankdumpbackend"
bucket_name = "foerderdatenbankdump"


@app.function(
    secrets=[
        modal.Secret.from_local_environ(
            [
                "POSTGRES_CONN_STR",
                "DESTINATION__FILESYSTEM__CREDENTIALS__ENDPOINT_URL",
                "DESTINATION__FILESYSTEM__CREDENTIALS__AWS_ACCESS_KEY_ID",
                "DESTINATION__FILESYSTEM__CREDENTIALS__AWS_SECRET_ACCESS_KEY",
            ]
        ),
    ],
    schedule=modal.Cron("0 2 */2 * *"),
    timeout=3600,
)
def crawl():
    local_license_file_name = "LICENSE-DATA"

    postgres_conn_str = os.getenv("POSTGRES_CONN_STR")
    assert postgres_conn_str

    engine = create_engine(postgres_conn_str)

    session = boto3.session.Session()
    client = session.client(
        "s3",
        endpoint_url=os.getenv("DESTINATION__FILESYSTEM__CREDENTIALS__ENDPOINT_URL"),
        aws_access_key_id=os.getenv(
            "DESTINATION__FILESYSTEM__CREDENTIALS__AWS_ACCESS_KEY_ID"
        ),
        aws_secret_access_key=os.getenv(
            "DESTINATION__FILESYSTEM__CREDENTIALS__AWS_SECRET_ACCESS_KEY"
        ),
    )

    dlt.config.register_provider(cfg_provider)

    pipeline = dlt.pipeline(
        pipeline_name="fundingcrawler",
        destination=dlt.destinations.postgres(postgres_conn_str),
        dataset_name=dataset_name,
    )

    scraping_host = create_pipeline_runner(
        pipeline, FundingSpider, batch_size=50, scrapy_settings=scrapy_settings
    )

    # https://dlthub.com/docs/general-usage/incremental-loading#scd2-strategy
    scraping_host.run(
        columns=FundingProgramSchema,
        write_disposition={
            "disposition": "merge",
            "strategy": "scd2",
            "validity_column_names": ["on_website_from", "on_website_to"],
            "row_version_column_name": "checksum",
        },
    )

    columns = list(FundingProgramSchema.__annotations__.keys())

    df = pl.read_database(
        query=gen_query(f"{dataset_name}.{dataset_name}", columns),
        connection=engine.connect(),
        execute_options={"parameters": []},
        schema_overrides=pydantic_to_polars_schema(FundingProgramSchema),
        infer_schema_length=None,
        batch_size=10000,
    )

    assert len(list(df["id_hash"].unique())) == len(df), "id_hash is not unique!"

    search_url = "https://www.foerderdatenbank.de/SiteGlobals/FDB/Forms/Suche/Foederprogrammsuche_Formular.html?resourceId=0065e6ec-5c0a-4678-b503-b7e7ec435dfd&input_=23adddb0-dcf7-4e32-96f5-93aec5db2716&pageLocale=de&filterCategories=FundingProgram"
    hits_count = get_hits_count(search_url)
    assert (
        abs(len(df.filter(pl.col("deleted") == False)) - hits_count) <= 4  # noqa: E712
    ), f"Scraped items do not approx. equal amount displayed on website {len(df.filter(pl.col("deleted") == False))}, {hits_count}"  # noqa: E712

    min_cols = [
        "id_hash",
        "deleted",
        "on_website_from",
        "url",
        "title",
        "description",
        "more_info",
        "legal_basis",
        "funding_type",
        "funding_area",
        "funding_location",
        "eligible_applicants",
        "funding_body",
    ]
    format_cols = ["description", "more_info", "legal_basis"]

    mindf = df[min_cols]
    print("formatting columns...")
    for col in format_cols:
        mindf = mindf.with_columns(pl.col(col).map_elements(md, return_dtype=pl.String))

    with open(local_license_file_name, "w") as f:
        f.write(license_content)

    for ext in ["csv", "parquet"]:
        local_data_name = f"data.{ext}"
        local_data_min_format = f"min_data_format.{ext}"
        local_zip_name = f"{ext}_data.zip"
        remote_zip_name = f"data/{local_zip_name}"

        if ext == "csv":
            print("handling list columns")
            for col in mindf.columns:
                if mindf[col].dtype == pl.List:
                    mindf = mindf.with_columns(
                        pl.col(col)
                        .list.join(", ")
                        .alias(col)  # Use original column name
                    )

            mindf.write_csv(local_data_min_format)
        elif ext == "parquet":
            df.write_parquet(local_data_name)
            mindf.write_parquet(local_data_min_format)
        else:
            raise

        with zipfile.ZipFile(local_zip_name, "w") as zipf:
            if ext != "csv":
                zipf.write(local_data_name, os.path.basename(local_data_name))
            zipf.write(local_data_min_format, os.path.basename(local_data_min_format))
            zipf.write(
                local_license_file_name, os.path.basename(local_license_file_name)
            )

        print(
            f"Uploading {local_zip_name} to {remote_zip_name} in bucket {bucket_name}"
        )

        client.upload_file(
            local_zip_name,
            bucket_name,
            remote_zip_name,
            ExtraArgs={"ACL": "public-read"},
        )

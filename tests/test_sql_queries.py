from funding_crawler.helpers import (
    gen_query,
    gen_comp_a,
    gen_comp_b,
    gen_comp_c,
    pydantic_to_polars_schema,
)
import random
import polars as pl
from sqlalchemy import create_engine
import os
from funding_crawler.models import FundingProgramSchema

dataset_name = "foerderdatenbankdumpbackend"
bucket_name = "cdl-foerderdatenbank-dump"
columns = list(FundingProgramSchema.__annotations__.keys())

postgres_conn_str = os.getenv("POSTGRES_CONN_STR")

engine = create_engine(postgres_conn_str)


def test_gen_comp_a():
    df = pl.read_database(
        query=gen_comp_a(f"{dataset_name}.{dataset_name}"),
        connection=engine.connect(),
        execute_options={"parameters": []},
        infer_schema_length=None,
        batch_size=10000,
    )
    assert len(df) > 1

    assert len(list(df["agg_id"].unique())) == len(df), "agg_id is not unique!"


def test_gen_comp_b():
    df = pl.read_database(
        query=gen_comp_b(f"{dataset_name}.{dataset_name}", columns),
        connection=engine.connect(),
        execute_options={"parameters": []},
        schema_overrides=pydantic_to_polars_schema(FundingProgramSchema),
        infer_schema_length=None,
        batch_size=10000,
    )
    assert len(df) > 1

    assert len(list(df["new_id_hash"].unique())) == len(
        df
    ), "new_id_hash is not unique!"

    assert None not in list(df["on_website_from"].unique())


def test_gen_comp_c():
    df = pl.read_database(
        query=gen_comp_c(f"{dataset_name}.{dataset_name}", columns),
        connection=engine.connect(),
        execute_options={"parameters": []},
        schema_overrides=pydantic_to_polars_schema(FundingProgramSchema),
        infer_schema_length=None,
        batch_size=10000,
    )
    assert len(df) > 1

    df_with_lengths = df.with_columns(
        pl.col("previous_update_dates").list.len().alias("list_length")
    )

    assert len(list(df["agg_id"].unique())) == len(df), "agg_id is not unique!"

    all_length_one = df_with_lengths.filter(pl.col("list_length") == 1)
    assert len(all_length_one) < len(df)


def test_join():
    df_retired = pl.read_database(
        query=gen_comp_c(f"{dataset_name}.{dataset_name}", columns),
        connection=engine.connect(),
        execute_options={"parameters": []},
        schema_overrides=pydantic_to_polars_schema(FundingProgramSchema),
        infer_schema_length=None,
        batch_size=10000,
    )

    df_new = pl.read_database(
        query=gen_comp_b(f"{dataset_name}.{dataset_name}", columns),
        connection=engine.connect(),
        execute_options={"parameters": []},
        schema_overrides=pydantic_to_polars_schema(FundingProgramSchema),
        infer_schema_length=None,
        batch_size=10000,
    )

    ids_retired = list(df_retired["agg_id"].unique())
    ids_new = list(df_new["new_id_hash"].unique())

    matching_ids = set(ids_retired).intersection(set(ids_new))

    assert len(list(matching_ids)) > 0


def test_gen_query_complete():
    df = pl.read_database(
        query=gen_query(f"{dataset_name}.{dataset_name}", columns),
        connection=engine.connect(),
        execute_options={"parameters": []},
        schema_overrides=pydantic_to_polars_schema(FundingProgramSchema),
        infer_schema_length=None,
        batch_size=10000,
    )
    assert len(df) > 1

    print(df.head())
    print(len(df))

    assert len(list(df["id_hash"].unique())) == len(df), "id_hash is not unique!"
    assert len(list(df["id_url"].unique())) == len(df), "id_url is not unique!"

    deleted = list(df.filter(pl.col("deleted"))["id_url"])
    assert len(deleted) > 0
    assert None not in deleted

    assert (
        len(
            df.filter(pl.col("deleted") == True).filter(  # noqa: E712
                pl.col("on_website_from").is_null()
            )
        )
        == 0
    )

    print("deleted: ", len(deleted))

    # get random deleted
    random_deleted = random.choice(deleted)
    print(random_deleted)

    active = list(df.filter(pl.col("deleted") == False)["id_url"])  # noqa: E712
    assert len(active) > 0
    assert None not in active
    print("active: ", len(active))

    present = df.filter(pl.col("deleted") == False)  # noqa: E712

    assert len(present.filter(pl.col("last_updated").is_null())) < len(present)
    assert len(present.filter(pl.col("previous_update_dates").is_null())) < len(present)

    assert len(present.filter(pl.col("last_updated") < pl.col("on_website_from"))) == 0

import requests
import zipfile
import io
from concurrent.futures import ThreadPoolExecutor
import polars as pl
from tqdm import tqdm


def test_not_deleted():
    data_url = "https://cdl-foerderdatenbank-dump.fsn1.your-objectstorage.com/data/parquet_data.zip"
    response = requests.get(data_url)
    response.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        file_name = z.namelist()[0]
        with z.open(file_name) as f:
            df = pl.read_parquet(f)

    current_df = df.filter(pl.col("deleted") == False)  # noqa: E712

    def is_not_404(url):
        try:
            response = requests.head(url, allow_redirects=True, timeout=5)
            return response.status_code != 404
        except requests.RequestException:
            return False

    def check_urls_concurrently(urls, max_workers=5):
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            results = list(
                tqdm(
                    executor.map(is_not_404, urls),
                    total=len(urls),
                    desc="Checking URLs",
                )
            )
        return results

    urls = current_df["url"].to_list()
    validity_results = check_urls_concurrently(urls)

    current_df = current_df.with_columns(pl.Series("is_valid", validity_results))

    invalid_urls_df = current_df.filter(pl.col("is_valid") == False)  # noqa: E712
    print("Invalid URLs:")
    print(invalid_urls_df["url"].to_list())
    print(invalid_urls_df["last_updated"].to_list())

    assert current_df["is_valid"].all(), "Not all URLs in current_df are valid!"

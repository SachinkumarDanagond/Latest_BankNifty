import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from kiteconnect import KiteConnect


ROOT = Path(__file__).resolve().parent


def load_credentials() -> tuple[str, str]:
    values: dict[str, str] = {}
    for line in (ROOT / "credentials.txt").read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()

    api_key = os.getenv("KITE_API_KEY", values.get("api_key", "zv7zhx52bee3u1rg"))
    api_secret = os.getenv("KITE_API_SECRET", values.get("api_secret", "f38z70utz8xlkbybao307poov08sfov0"))
    if not api_key or not api_secret:
        raise RuntimeError("Set KITE_API_KEY and KITE_API_SECRET or fill credentials.txt")
    return api_key, api_secret


def get_request_token(callback_url: str) -> str:
    parsed = urlparse(callback_url)
    params = parse_qs(parsed.query)
    request_token = params.get("request_token", [""])[0].strip()
    if not request_token:
        raise ValueError("No request_token found in the callback URL. Check that the redirect URL is complete.")
    return request_token


def main() -> None:
    api_key, api_secret = load_credentials()
    kite = KiteConnect(api_key=api_key)

    login_url = kite.login_url()
    print("Open this URL in your browser and log in:")
    print(login_url)
    print()

    callback_url = input("Paste the full callback URL here: ").strip()
    request_token = get_request_token(callback_url)

    print()
    print("request_token =", request_token)
    print()

    session = kite.generate_session(request_token, api_secret)
    access_token = session["access_token"]

    print("access_token =", access_token)
    print()
    print("Set this in your terminal before running the bot:")
    print(f"$env:KITE_ACCESS_TOKEN = \"{access_token}\"")


if __name__ == "__main__":
    main()

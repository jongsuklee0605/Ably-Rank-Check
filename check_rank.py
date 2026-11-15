name: Canmake Ably Rank Check

on:
  schedule:
    - cron: "0 * * * *"
  workflow_dispatch:

jobs:
  check:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    env:
      # 안드로이드: chromium + "Pixel 7"  /  아이폰: webkit + "iPhone 14"
      BROWSER: "chromium"
      DEVICE: "Pixel 7"
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install Python deps
        run: pip install -r requirements.txt

      - name: Cache Playwright browsers
        uses: actions/cache@v4
        with:
          path: ~/.cache/ms-playwright
          key: playwright-${{ runner.os }}-${{ env.BROWSER }}-${{ hashFiles('requirements.txt') }}

      - name: Install browser
        run: python -m playwright install "$BROWSER"

      - name: Install system deps (best effort)
        continue-on-error: true
        timeout-minutes: 4
        run: |
          for i in 1 2 3; do
            python -m playwright install-deps "$BROWSER" && exit 0
            echo "install-deps failed (attempt $i), retrying..."
            sleep 15
          done
          exit 1

      - name: Check ranking
        run: xvfb-run -a python check_rank.py
        env:
          SLACK_WEBHOOK_URL: ${{ secrets.SLACK_WEBHOOK_URL }}
          MARKET_ID: "116160"
          BRAND_NAME: "Canmake"
          TOP_N: "100"
          STALE_SEC: "120"
          MAX_SEC: "360"
          SCAN_TRIES: "1"
          BLOCK_IMAGES: "false"
          # 이름|URL|카테고리|TOP_N|대기초(이 탭을 누른 뒤 몇 초 지나야 다음으로)
          RANKINGS: |
            뷰티 랭킹|https://mobile.a-bly.com/ranking|뷰티|100|300
            전체 랭킹|https://mobile.a-bly.com/ranking||100|

      - name: Upload debug files
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: debug-files
          path: |
            debug_*.png
            debug_*.txt
          if-no-files-found: ignore
          retention-days: 3

import os
import re
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, parse_qs

import requests
from playwright.sync_api import sync_playwright

BRAND_NAME = os.getenv("BRAND_NAME", "Canmake")
REPORT_TITLE = os.getenv("REPORT_TITLE", "에이블리 랭킹 체크")
BRAND_KEYWORDS = ["캔메이크", "canmake"]
DEFAULT_TOP_N = int(os.getenv("TOP_N", "100"))
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"].strip()
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"   # true면 슬랙 전송 안 함 (시험용)

# 브라우저/기기 설정
#   BROWSER: chromium / webkit
#   CHANNEL: chromium일 때 "chrome"이면 실제 구글 크롬 사용 (비우면 기본 크로미움)
#   DEVICE : "Pixel 7", "iPhone 14" 등
BROWSER = os.getenv("BROWSER", "chromium").strip().lower()
CHANNEL = os.getenv("CHANNEL", "").strip()
DEVICE = os.getenv("DEVICE", "Pixel 7").strip()

# 불필요한 요청 차단 (통계 전송 서버만 차단, 이미지는 기본 허용)
BLOCK_IMAGES = os.getenv("BLOCK_IMAGES", "false").lower() == "true"
BLOCK_HOSTS = ["api2.amplitude.com"]

# 로딩 대기 설정
STALE_SEC =

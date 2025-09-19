import os

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL","http://your-prometheus-server:9090")
GRAFANA_URL = os.getenv("GRAFANA_URL", "http://localhost:3000")
GRAFANA_API_KEY = os.getenv("GRAFANA_API_KEY")
TIME_FROM = os.getenv("TIME_FROM", "now-6h")
TIME_TO = os.getenv("TIME_TO", "now")
TIME_TO_CSV = os.getenv("TIME_TO_CSV", "now")
EMAIL_FROM = os.getenv("EMAIL_FROM")
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
A4_WIDTH_PX = 2480
A4_HEIGHT_PX = 3508
A4_BG_COLOR = "white"

EXCLUDED_TITLES = ["Report Button", "Another panel"]
EXCLUDED_TITLES_LOWER = [t.strip().lower() for t in EXCLUDED_TITLES]

HEADERS = {"Authorization": f"Bearer {GRAFANA_API_KEY}"}

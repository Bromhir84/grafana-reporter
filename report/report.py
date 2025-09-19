import os
import io
import csv
import time
import logging
from datetime import datetime
from email.message import EmailMessage
import smtplib
import pandas as pd
from PIL import Image
import requests

from .grafana_utils import (
    extract_uid_from_url,
    clone_dashboard_without_panels,
    delete_dashboard,
    paginate_to_a4,
    generate_pdf_from_pages,
    extract_grafana_vars,
    resolve_grafana_vars,
)
from .prometheus_utils import (
    query_prometheus_range,
    extract_metric,
    compute_range_from_env,
)
from .config import EXCLUDED_TITLES

logger = logging.getLogger(__name__)

def process_report(dashboard_url: str, email_to: str = None, excluded_titles=None):
    """
    Generate a Grafana report:
      1. Clone dashboard (excluding some panels).
      2. Rebuild table panels as CSVs via Prometheus queries.
      3. Render dashboard as PDF.
      4. Email PDF and CSVs if email_to is provided.
    """
    excluded_titles = excluded_titles or EXCLUDED_TITLES
    temp_uid, csv_files, pdf_path = None, [], None

    try:
        # --- Step 1: Clone dashboard and extract table panels ---
        dashboard_uid = extract_uid_from_url(dashboard_url)
        temp_uid, table_panels, GRAFANA_VARS = clone_dashboard_without_panels(
            dashboard_uid, excluded_titles
        )

        # Step 2: Build CSVs for table panels
        start_dt, end_dt = compute_range_from_env(os.getenv("TIME_FROM", "now-6h"), os.getenv("TIME_TO_CSV", "now"))
        logger.info(f"Querying Prometheus from {start_dt} to {end_dt}")

        for panel in table_panels:
            logger.info(f"Rebuilding table panel: {panel['title']}")
            panel_df = None

            for expr in panel["queries"]:
                expr_resolved = resolve_grafana_vars(expr, GRAFANA_VARS, start_dt, end_dt)
                metric_name = extract_metric(expr_resolved)
                range_seconds = int((end_dt - start_dt).total_seconds())
                logger.info(
                    f"Querying Prometheus for panel '{panel['title']}': {expr_resolved}"
                )

                try:
                    results = query_prometheus_range(expr_resolved, start=start_dt, end=end_dt, step=range_seconds)
                except Exception as e:
                    logger.error(f"Prometheus query failed for {expr_resolved}: {e}")
                    continue

                rows = []
                for r in results.get("data", {}).get("result", []):
                    metric_labels = r.get("metric", {})
                    key = metric_labels.get("project") or metric_labels.get("department") or "unknown"

                    if r.get("values"):
                        _, value = r["values"][-1]
                        rows.append({"key": key, metric_name: float(value)})

                if rows:
                    df = pd.DataFrame(rows)
                    panel_df = df if panel_df is None else pd.merge(panel_df, df, on="key", how="outer")

            if panel_df is not None and not panel_df.empty:
                panel_df = panel_df.fillna(0)
                panel_df.rename(columns={"key": "project_or_department"}, inplace=True)
                csv_path = f"/tmp/{panel['title'].replace(' ', '_')}.csv"
                panel_df.to_csv(csv_path, index=False)
                csv_files.append(csv_path)
                logger.info(f"CSV saved for panel '{panel['title']}': {csv_path}")
            else:
                logger.warning(f"No data for panel '{panel['title']}'")

        # --- Step 3: Render dashboard as PDF ---
        render_url = (
            f"{os.getenv('GRAFANA_URL')}/render/d/{temp_uid}"
            f"?kiosk&width=2480&height=10000&theme=light&tz=UTC"
            f"&from={os.getenv('TIME_FROM', 'now-6h')}&to={os.getenv('TIME_TO', 'now')}"
        )
        logger.info(f"Rendering dashboard at {render_url}")
        r = requests.get(render_url, headers={"Authorization": f"Bearer {os.getenv('GRAFANA_API_KEY')}"}, stream=True, timeout=60)
        r.raise_for_status()

        img = Image.open(io.BytesIO(r.content))
        pages = paginate_to_a4(img)

        pdf_path = f"/tmp/grafana_report_{temp_uid}.pdf"
        generate_pdf_from_pages(pages, pdf_path)
        logger.info(f"PDF saved: {pdf_path}")

        # --- Step 4: Email results ---
        if email_to:
            msg = EmailMessage()
            msg["Subject"] = f"Grafana Report - {temp_uid} - {datetime.now().strftime('%Y-%m-%d')}"
            msg["From"] = os.getenv("EMAIL_FROM")
            msg["To"] = email_to

            # Attach PDF
            if pdf_path and os.path.exists(pdf_path):
                with open(pdf_path, "rb") as f:
                    msg.add_attachment(f.read(), maintype="application", subtype="pdf", filename=f"{temp_uid}.pdf")

            # Attach CSVs
            for csv_file in csv_files:
                with open(csv_file, "rb") as f:
                    msg.add_attachment(f.read(), maintype="text", subtype="csv", filename=os.path.basename(csv_file))

            # Send email
            with smtplib.SMTP(os.getenv("SMTP_HOST"), int(os.getenv("SMTP_PORT", 587))) as server:
                server.starttls()
                username = os.getenv("SMTP_USERNAME")
                password = os.getenv("SMTP_PASSWORD")
                if username and password:
                    server.login(username, password)
                server.send_message(msg)

            logger.info(f"Email sent to {email_to}")

        logger.info(f"Report completed successfully: PDF + {len(csv_files)} CSVs")

    except Exception as e:
        logger.error(f"Error during report generation: {e}")

    finally:
        if temp_uid:
            delete_dashboard(temp_uid)
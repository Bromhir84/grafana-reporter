import logging
from datetime import datetime
import os
import pytz
from ..config import TIME_FROM, TIME_TO, TIME_TO_CSV
from .grafana_utils import clone_dashboard_without_panels, delete_dashboard, paginate_to_a4, generate_pdf_from_pages
from .prometheus_utils import (
    compute_range_from_env,
    extract_uid_from_url,
    resolve_grafana_vars,
    query_prometheus_range,
    extract_metric
)
from .email_utils import send_email
from .recording_rule_backfill import RecordingRuleBackfill

import re
from PIL import Image
import io
import pandas as pd

logger = logging.getLogger(__name__)


def process_report(dashboard_url: str, email_to: str = None, excluded_titles=None):
    excluded_titles = excluded_titles or []
    temp_uid, csv_files, pdf_path, dashboard_tz = None, [], None, None

    # Initialize recording rule backfiller
    backfiller = RecordingRuleBackfill("/path/to/recording_rules.yaml")

    try:
        # --- Clone dashboard and extract timezone ---
        dashboard_uid = extract_uid_from_url(dashboard_url)
        temp_uid, table_panels, GRAFANA_VARS, dash_json = clone_dashboard_without_panels(
            dashboard_uid, excluded_titles, return_json=True
        )

        dashboard_tz = dash_json.get("timezone", "UTC")
        logger.info(f"Dashboard timezone = {dashboard_tz}")

        # --- Compute range ---
        start_dt, end_dt = compute_range_from_env(TIME_FROM, TIME_TO_CSV)
        logger.info(f"Querying Prometheus from {start_dt} to {end_dt}")

        # --- Loop panels ---
        for panel in table_panels:
            logger.info(f"Rebuilding table panel: {panel['title']}")
            panel_df = None

            for expr in panel["queries"]:
                expr_resolved = resolve_grafana_vars(expr, GRAFANA_VARS, start_dt, end_dt)
                metric_name = extract_metric(expr_resolved)
                range_seconds = int((end_dt - start_dt).total_seconds())
                logger.info(f"Querying Prometheus: {expr_resolved}")

                # Query Prometheus
                try:
                    results = query_prometheus_range(expr_resolved, start=start_dt, end=end_dt, step=range_seconds)
                except Exception as e:
                    logger.error(f"Prometheus query failed for {expr_resolved}: {e}")
                    continue

                results_list = results.get("data", {}).get("result", [])
                rows = []

                # --- Backfill if Prometheus returned empty ---
                if not results_list:
                    recording_rule_names = set(backfiller.rules_map.keys())
                    matches = re.findall(r"[a-zA-Z0-9_:]+", expr_resolved)
                    found_rules = [m for m in matches if m in recording_rule_names]

                    if found_rules:
                        found_rule = found_rules[0]
                        logger.info(f"Detected recording rule in PromQL: {found_rule}")
                        results = backfiller.backfill_rule(
                            record_name=found_rule,
                            start=start_dt,
                            end=end_dt,
                            step=range_seconds
                        )
                        results_list = results.get("data", {}).get("result", [])

                # --- Convert results to rows ---
                for r in results_list:
                    metric_labels = r.get("metric", {})
                    project = metric_labels.get("project", "unknown")
                    department = metric_labels.get("department", "unknown")
                    if r.get("values"):
                        _, value = r["values"][-1]
                        rows.append({
                            "project": project,
                            "department": department,
                            metric_name: float(value)
                        })

                # --- Fallback to zeros if still empty ---
                if not rows:
                    known_keys = (
                        panel_df[["project", "department"]].drop_duplicates().to_dict("records")
                        if panel_df is not None else [{"project": "unknown", "department": "unknown"}]
                    )
                    for k in known_keys:
                        rows.append({
                            "project": k["project"],
                            "department": k["department"],
                            metric_name: 0.0
                        })

                # --- Merge with previous panel_df ---
                df = pd.DataFrame(rows)
                panel_df = df if panel_df is None else pd.merge(
                    panel_df, df, on=["project", "department"], how="outer"
                )

            # --- Save CSV if panel_df has data ---
            if panel_df is not None and not panel_df.empty:
                panel_df = panel_df.fillna(0)
                panel_df.rename(columns={
                    "Runai Allocated Gpu Count": "GPU allocation hours.",
                    "Runai Allocated Millicpus": "Allocated mCPUs",
                    "Runai Used Cpu Cores": "CPU Usage (cores)",
                    "Runai Used Memory Bytes": "Memory Usage (bytes)",
                }, inplace=True)
                safe_title = re.sub(r'[^A-Za-z0-9_\-]', '_', panel['title'])
                csv_path = os.path.join("/tmp", f"{safe_title}.csv")
                os.makedirs(os.path.dirname(csv_path), exist_ok=True) 
                panel_df.to_csv(csv_path, index=False, sep=';', decimal=",")
                csv_files.append(csv_path)
                logger.info(f"CSV saved for panel '{panel['title']}': {csv_path}")
            else:
                logger.warning(f"No data for panel '{panel['title']}'")

        # --- Render dashboard as PDF ---
        render_url = (
            f"{os.getenv('GRAFANA_URL')}/render/d/{temp_uid}"
            f"?kiosk&width=2480&height=10000&theme=light"
            f"&tz=Europe/Amsterdam&from={TIME_FROM}&to={TIME_TO}"
        )
        logger.info(f"Rendering dashboard at {render_url}")

        import requests
        r = requests.get(render_url, stream=True, headers={"Authorization": f"Bearer {os.getenv('GRAFANA_API_KEY')}"}, timeout=60)
        r.raise_for_status()

        img = Image.open(io.BytesIO(r.content))
        pages = paginate_to_a4(img)

        pdf_path = f"/tmp/grafana_report_{temp_uid}.pdf"
        generate_pdf_from_pages(pages, pdf_path)
        logger.info(f"PDF saved: {pdf_path}")

        # --- Send Email ---
        if email_to:
            send_email(pdf_path, csv_files, temp_uid, email_to)

        logger.info(f"Report completed successfully: PDF + {len(csv_files)} CSVs")

    except Exception as e:
        logger.error(f"Error during report generation: {e}")

    finally:
        if temp_uid:
            delete_dashboard(temp_uid)
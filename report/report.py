import logging
from datetime import datetime, timedelta
import os
import re
from PIL import Image
import io
import pandas as pd
import concurrent.futures

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

logger = logging.getLogger(__name__)

def process_report(dashboard_url: str, email_to: str = None, excluded_titles=None, chunk_hours: int = 24, max_workers: int = 4):
    excluded_titles = excluded_titles or []
    temp_uid, csv_files, pdf_path, dashboard_tz = None, [], None, None

    backfiller = RecordingRuleBackfill(os.path.join(os.path.dirname(__file__), "runai.yaml"))
    backfill_cache = {}  # (rule_name, start, end) -> DataFrame

    try:
        # --- Clone dashboard and extract timezone ---
        dashboard_uid = extract_uid_from_url(dashboard_url)
        temp_uid, table_panels, GRAFANA_VARS, dash_json = clone_dashboard_without_panels(
            dashboard_uid, excluded_titles, return_json=True
        )
        dashboard_tz = dash_json.get("timezone", "UTC")
        logger.info(f"Dashboard timezone = {dashboard_tz}")

        # --- Compute date range ---
        start_dt, end_dt = compute_range_from_env(TIME_FROM, TIME_TO_CSV)
        logger.info(f"Querying Prometheus from {start_dt} to {end_dt} in chunks of {chunk_hours}h")

        for panel in table_panels:
            logger.info(f"Rebuilding table panel: {panel['title']}")
            panel_df = None

            for expr in panel["queries"]:
                expr_resolved = resolve_grafana_vars(expr, GRAFANA_VARS, start_dt, end_dt)
                metric_name = extract_metric(expr_resolved)
                total_seconds = int((end_dt - start_dt).total_seconds())

                # --- Split time into chunks ---
                chunk_delta = timedelta(hours=chunk_hours)
                chunk_ranges = []
                chunk_start = start_dt
                while chunk_start < end_dt:
                    chunk_end = min(chunk_start + chunk_delta, end_dt)
                    chunk_ranges.append((chunk_start, chunk_end))
                    chunk_start = chunk_end

                # --- Function to query/backfill a single chunk ---
                def query_chunk(chunk_range):
                    cs, ce = chunk_range
                    logger.info(f"[Chunk {cs} -> {ce}] Querying {metric_name}")
                    try:
                        results = query_prometheus_range(expr_resolved, start=cs, end=ce, step=total_seconds)
                        results_list = results.get("data", {}).get("result", [])

                        if not results_list:
                            # Attempt to match metric_name to a recording rule
                            matching_rule = None
                            for rule_name in backfiller.rules_map:
                                if metric_name.lower().replace(" ", "_") in rule_name.lower():
                                    matching_rule = rule_name
                                    break

                            if matching_rule:
                                cache_key = (matching_rule, cs, ce)
                                if cache_key in backfill_cache:
                                    logger.info(f"[Chunk {cs} -> {ce}] Using cached backfill for {matching_rule}")
                                    return backfill_cache[cache_key]

                                logger.info(f"[Chunk {cs} -> {ce}] Backfilling {matching_rule}")
                                df = backfiller.backfill_rule_recursive(matching_rule, cs, ce, step=total_seconds)
                                backfill_cache[cache_key] = df
                                return df
                            else:
                                logger.warning(f"[Chunk {cs} -> {ce}] No backfilled data found for metric: {metric_name}")
                                return pd.DataFrame(columns=["project", "department", metric_name])
                        else:
                            return backfiller._prometheus_result_to_df(results, metric_name)

                    except Exception as e:
                        logger.error(f"[Chunk {cs} -> {ce}] Query failed for {metric_name}: {e}")
                        return pd.DataFrame(columns=["project", "department", metric_name])

                # --- Parallel chunk execution ---
                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                    chunk_dfs = list(executor.map(query_chunk, chunk_ranges))

                # --- Merge all chunks ---
                if chunk_dfs:
                    df = pd.concat(chunk_dfs, ignore_index=True)
                else:
                    df = pd.DataFrame(columns=["project", "department", metric_name])

                # --- Merge with panel_df ---
                panel_df = df if panel_df is None else pd.merge(
                    panel_df, df, on=["project", "department"], how="outer"
                )

            # --- Save CSV ---
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

        if email_to:
            send_email(pdf_path, csv_files, temp_uid, email_to)

        logger.info(f"Report completed successfully: PDF + {len(csv_files)} CSVs")

    except Exception as e:
        logger.error(f"Error during report generation: {e}")

    finally:
        if temp_uid:
            delete_dashboard(temp_uid)
import logging
from datetime import datetime
import os
from ..config import TIME_FROM, TIME_TO, TIME_TO_CSV
from .grafana_utils import clone_dashboard_without_panels, delete_dashboard, paginate_to_a4, generate_pdf_from_pages
from .prometheus_utils import (
    compute_range_from_env,
    extract_uid_from_url,
    resolve_grafana_vars,
    query_prometheus_instant,
    query_prometheus_range,
    extract_metric,
    compute_query_step_seconds,
    parse_duration_to_seconds,
)
from .email_utils import send_email

import re
from PIL import Image
import io
import pandas as pd

logger = logging.getLogger(__name__)


def process_report(dashboard_url: str, email_to: str = None, excluded_titles=None):
    excluded_titles = excluded_titles or []
    temp_uid, csv_files, pdf_path, dashboard_tz = None, [], None, None

    try:
        # --- Clone dashboard and extract timezone ---
        dashboard_uid = extract_uid_from_url(dashboard_url)
        temp_uid, table_panels, GRAFANA_VARS, dash_json = clone_dashboard_without_panels(
            dashboard_uid, excluded_titles, return_json=True  # update utils for this
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

            for query_spec in panel["queries"]:
                if isinstance(query_spec, str):
                    expr = query_spec
                    explicit_interval_seconds = None
                    instant_flag = None
                else:
                    expr = query_spec.get("expr", "")
                    instant_flag = query_spec.get("instant")
                    interval_ms = query_spec.get("interval_ms")
                    interval_from_text = parse_duration_to_seconds(query_spec.get("interval"))
                    min_step_seconds = parse_duration_to_seconds(query_spec.get("min_step"))
                    max_data_points = query_spec.get("max_data_points")

                    explicit_interval_seconds = None
                    if interval_ms not in (None, ""):
                        try:
                            explicit_interval_seconds = max(1, int(interval_ms) // 1000)
                        except (TypeError, ValueError):
                            explicit_interval_seconds = None
                    if explicit_interval_seconds is None:
                        explicit_interval_seconds = interval_from_text

                    if explicit_interval_seconds is None:
                        try:
                            max_points = int(max_data_points) if max_data_points not in (None, "") else 1000
                        except (TypeError, ValueError):
                            max_points = 1000
                        explicit_interval_seconds = compute_query_step_seconds(
                            start_dt,
                            end_dt,
                            max_points=max_points,
                            min_step=min_step_seconds or 60,
                        )

                if not expr:
                    continue

                expr_resolved = resolve_grafana_vars(
                    expr,
                    GRAFANA_VARS,
                    start_dt,
                    end_dt,
                    interval_seconds=explicit_interval_seconds,
                )
                if explicit_interval_seconds is None:
                    explicit_interval_seconds = compute_query_step_seconds(start_dt, end_dt)

                uses_subquery = bool(re.search(r"\[[^\]]+:[^\]]+\]", expr_resolved))
                use_range_mode = (instant_flag is False) if instant_flag is not None else uses_subquery
                metric_name = extract_metric(expr_resolved)
                mode = "range-last" if use_range_mode else "instant"
                logger.info(f"Querying Prometheus ({mode} @ {end_dt}): {expr_resolved}")

                try:
                    if use_range_mode:
                        results = query_prometheus_range(
                            expr_resolved,
                            start=start_dt,
                            end=end_dt,
                            step=explicit_interval_seconds,
                        )
                    else:
                        results = query_prometheus_instant(expr_resolved, eval_time=end_dt)
                except Exception as e:
                    logger.error(f"Prometheus query failed for {expr_resolved}: {e}")
                    continue

                rows = []
                for r in results.get("data", {}).get("result", []):
                    metric_labels = r.get("metric", {})
                    project = metric_labels.get("project", "unknown")
                    department = metric_labels.get("department", "unknown")

                    if use_range_mode and r.get("values"):
                        _, value = r["values"][-1]
                    elif r.get("value"):
                        _, value = r["value"]
                    else:
                        continue

                    rows.append({
                        "project": project,
                        "department": department,
                        metric_name: float(value)
                    })

                if rows:
                    df = pd.DataFrame(rows)

                    # Merge with previous results if needed
                    if panel_df is None:
                        panel_df = df
                    else:
                        panel_df = pd.merge(
                            panel_df, df,
                            on=["project", "department"],  # 👈 join on both
                            how="outer"
                        )

            if panel_df is not None and not panel_df.empty:
                panel_df = panel_df.fillna(0)
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
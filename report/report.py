import logging
from datetime import datetime, timezone
import os
import requests
from ..config import TIME_FROM, TIME_TO, TIME_TO_CSV, GRAFANA_URL, GRAFANA_API_KEY
from .grafana_utils import clone_dashboard_without_panels, delete_dashboard, paginate_to_a4, generate_pdf_from_pages
from .prometheus_utils import (
    compute_range_from_env,
    _seconds_to_prom_duration,
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
grafana_headers = {"Authorization": f"Bearer {GRAFANA_API_KEY}", "Content-Type": "application/json"}


def _query_grafana_range_last(
    expr: str,
    query_spec: dict,
    variables: dict,
    start_dt: datetime,
    end_dt: datetime,
    interval_seconds: int | None,
):
    """
    Query Grafana datasource backend directly and return last value per label set.
    This aligns execution with dashboard backend semantics better than raw Prometheus calls.
    """
    datasource = query_spec.get("datasource") if isinstance(query_spec, dict) else None
    if not datasource:
        return None

    if isinstance(datasource, str):
        if datasource.startswith("$"):
            return None
        datasource_obj = {"uid": datasource}
    elif isinstance(datasource, dict):
        datasource_obj = {k: v for k, v in datasource.items() if k in ("uid", "type") and v}
        if not datasource_obj:
            return None
    else:
        return None

    from_ms = int(start_dt.astimezone(timezone.utc).timestamp() * 1000)
    to_ms = int(end_dt.astimezone(timezone.utc).timestamp() * 1000)
    range_seconds = max(1, int((end_dt - start_dt).total_seconds()))
    ref_id = (query_spec.get("ref_id") if isinstance(query_spec, dict) else None) or "A"
    max_data_points = query_spec.get("max_data_points") if isinstance(query_spec, dict) else None
    interval_ms_payload = int(max(1, interval_seconds) * 1000) if interval_seconds is not None else None
    interval_text_payload = _seconds_to_prom_duration(interval_seconds) if interval_seconds is not None else None

    scoped_vars = {
        name: {"text": str(value), "value": value}
        for name, value in variables.items()
    }

    scoped_vars.update({
        "__range": {"text": _seconds_to_prom_duration(range_seconds), "value": _seconds_to_prom_duration(range_seconds)},
        "__range_s": {"text": str(range_seconds), "value": range_seconds},
        "__range_ms": {"text": str(range_seconds * 1000), "value": range_seconds * 1000},
    })

    if interval_ms_payload is not None and interval_text_payload is not None:
        scoped_vars.update({
            "__interval": {"text": interval_text_payload, "value": interval_text_payload},
            "__interval_ms": {"text": str(interval_ms_payload), "value": interval_ms_payload},
            "__rate_interval": {
                "text": _seconds_to_prom_duration(max(60, interval_seconds * 4)),
                "value": _seconds_to_prom_duration(max(60, interval_seconds * 4)),
            },
        })

    query_payload = {
        "refId": ref_id,
        "expr": expr,
        "datasource": datasource_obj,
        "instant": False,
        "range": True,
        "scopedVars": scoped_vars,
    }

    if interval_ms_payload is not None:
        query_payload["intervalMs"] = interval_ms_payload

    interval_text = query_spec.get("interval") if isinstance(query_spec, dict) else None
    if interval_text:
        query_payload["interval"] = str(interval_text)

    if max_data_points not in (None, ""):
        try:
            query_payload["maxDataPoints"] = int(max_data_points)
        except (TypeError, ValueError):
            pass

    payload = {
        "from": str(from_ms),
        "to": str(to_ms),
        "queries": [query_payload],
    }

    target_format = query_spec.get("format") if isinstance(query_spec, dict) else None
    if target_format:
        query_payload["format"] = target_format
    legend_format = query_spec.get("legend_format") if isinstance(query_spec, dict) else None
    if legend_format:
        query_payload["legendFormat"] = legend_format
    editor_mode = query_spec.get("editor_mode") if isinstance(query_spec, dict) else None
    if editor_mode:
        query_payload["editorMode"] = editor_mode

    response = requests.post(f"{GRAFANA_URL}/api/ds/query", headers=grafana_headers, json=payload, timeout=60)
    response.raise_for_status()
    data = response.json()

    logger.info(
        "Grafana ds/query refId=%s intervalMs=%s interval=%s maxDataPoints=%s format=%s",
        ref_id,
        str(query_payload.get("intervalMs")),
        str(query_payload.get("interval")),
        str(query_payload.get("maxDataPoints")),
        str(query_payload.get("format")),
    )

    result_entry = data.get("results", {}).get(ref_id)
    if not result_entry:
        results_map = data.get("results", {})
        if results_map:
            result_entry = next(iter(results_map.values()))
    if not result_entry:
        return []

    executed_query = ((result_entry.get("meta") or {}).get("custom") or {}).get("executedQueryString")
    if executed_query:
        logger.info("Grafana executed query (%s): %s", ref_id, executed_query)

    frames = result_entry.get("frames", [])
    reducer = (query_spec.get("reducer") if isinstance(query_spec, dict) else None) or "lastNotNull"
    logger.info("Grafana ds/query refId=%s returned %d frames", ref_id, len(frames))

    def reduce_values(values, reducer_name, timestamps=None):
        pairs = []
        for i, value in enumerate(values):
            if value is None:
                continue
            ts = None
            if timestamps is not None and i < len(timestamps):
                ts = timestamps[i]
            pairs.append((ts, float(value)))

        if not pairs:
            return None

        name = str(reducer_name or "lastNotNull")
        numeric_values = [v for _, v in pairs]
        if name in ("mean", "avg"):
            return sum(numeric_values) / len(numeric_values)
        if name == "sum":
            return sum(numeric_values)
        if name == "min":
            return min(numeric_values)
        if name == "max":
            return max(numeric_values)
        if name in ("first", "firstNotNull"):
            return numeric_values[0]
        if name in ("last", "lastNotNull"):
            return numeric_values[-1]
        # Default and common table reducer.
        return numeric_values[-1]

    parsed_rows = []
    for frame_idx, frame in enumerate(frames):
        schema_fields = frame.get("schema", {}).get("fields", [])
        values_matrix = frame.get("data", {}).get("values", [])
        if not schema_fields or not values_matrix:
            continue

        if frame_idx < 3:
            logger.info(
                "Grafana frame[%d] fields=%s",
                frame_idx,
                [f"{field.get('name', '')}:{field.get('type', '')}" for field in schema_fields],
            )

        field_names = [field.get("name", "") for field in schema_fields]
        numeric_indexes = [idx for idx, field in enumerate(schema_fields) if field.get("type") == "number"]
        string_indexes = [idx for idx, field in enumerate(schema_fields) if field.get("type") == "string"]
        time_indexes = [idx for idx, field in enumerate(schema_fields) if field.get("type") == "time"]
        time_values = values_matrix[time_indexes[0]] if time_indexes and time_indexes[0] < len(values_matrix) else None
        row_count = max((len(col) for col in values_matrix), default=0)

        for idx in numeric_indexes:
            field = schema_fields[idx]
            col_values = values_matrix[idx] if idx < len(values_matrix) else []
            field_labels = field.get("labels", {}) or {}

            # Wide frame: labels are attached to numeric field metadata.
            if field_labels.get("project") or field_labels.get("department"):
                reduced_value = reduce_values(col_values, reducer, time_values)
                if reduced_value is None:
                    continue
                parsed_rows.append({
                    "metric": field_labels,
                    "value": float(reduced_value),
                })
                continue

            # Long frame: project/department are regular string columns per row.
            series_by_key = {}
            for row_idx in range(row_count):
                value = col_values[row_idx] if row_idx < len(col_values) else None
                if value is None:
                    continue
                ts = time_values[row_idx] if time_values is not None and row_idx < len(time_values) else None

                labels = dict(field_labels)
                for sidx in string_indexes:
                    label_name = field_names[sidx]
                    label_value_col = values_matrix[sidx] if sidx < len(values_matrix) else []
                    label_value = label_value_col[row_idx] if row_idx < len(label_value_col) else None
                    if label_name in ("project", "department") and label_value is not None:
                        labels[label_name] = str(label_value)

                key = (labels.get("project", "unknown"), labels.get("department", "unknown"))
                entry = series_by_key.setdefault(key, {"metric": labels, "values": []})
                entry["values"].append((ts, value))

            for entry in series_by_key.values():
                series_timestamps = [ts for ts, _ in entry["values"]]
                series_values = [v for _, v in entry["values"]]
                reduced_value = reduce_values(series_values, reducer, series_timestamps)
                if reduced_value is None:
                    continue
                parsed_rows.append({
                    "metric": entry["metric"],
                    "value": float(reduced_value),
                })

    deduped_rows = {}
    for row in parsed_rows:
        labels = row.get("metric", {})
        key = (
            labels.get("project", "unknown"),
            labels.get("department", "unknown"),
        )
        # Prefer the last parsed row for a label pair to avoid duplicate-key fanout in downstream merges.
        deduped_rows[key] = row

    if len(deduped_rows) != len(parsed_rows):
        logger.info(
            "Grafana ds/query refId=%s deduplicated rows from %d to %d",
            ref_id,
            len(parsed_rows),
            len(deduped_rows),
        )

    return list(deduped_rows.values())


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
        range_seconds = int((end_dt - start_dt).total_seconds())
        logger.info(f"Querying Prometheus from {start_dt} to {end_dt}")
        logger.info(f"Computed range seconds = {range_seconds}")

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

                payload_interval_seconds = None
                if isinstance(query_spec, dict):
                    if interval_ms not in (None, ""):
                        try:
                            payload_interval_seconds = max(1, int(interval_ms) // 1000)
                        except (TypeError, ValueError):
                            payload_interval_seconds = None
                    if payload_interval_seconds is None:
                        payload_interval_seconds = interval_from_text

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
                if use_range_mode:
                    reducer_name = query_spec.get("reducer") if isinstance(query_spec, dict) else None
                    logger.info(
                        f"Querying Prometheus ({mode} @ {end_dt}, step={explicit_interval_seconds}s, reducer={reducer_name or 'lastNotNull'}): {expr_resolved}"
                    )
                else:
                    logger.info(f"Querying Prometheus ({mode} @ {end_dt}): {expr_resolved}")

                try:
                    grafana_rows = None
                    if use_range_mode and isinstance(query_spec, dict):
                        try:
                            grafana_rows = _query_grafana_range_last(
                                expr,
                                query_spec,
                                GRAFANA_VARS,
                                start_dt,
                                end_dt,
                                payload_interval_seconds,
                            )
                            logger.info("Query mode: grafana-ds-query")
                        except Exception as grafana_error:
                            logger.warning(f"Grafana datasource query fallback to Prometheus: {grafana_error}")

                    if use_range_mode:
                        if grafana_rows is None:
                            results = query_prometheus_range(
                                expr_resolved,
                                start=start_dt,
                                end=end_dt,
                                step=explicit_interval_seconds,
                                align_to_step=True,
                            )
                        else:
                            results = None
                    else:
                        results = query_prometheus_instant(expr_resolved, eval_time=end_dt)
                except Exception as e:
                    logger.error(f"Prometheus query failed for {expr_resolved}: {e}")
                    continue

                rows = []
                if use_range_mode and grafana_rows is not None:
                    for row in grafana_rows:
                        metric_labels = row.get("metric", {})
                        rows.append({
                            "project": metric_labels.get("project", "unknown"),
                            "department": metric_labels.get("department", "unknown"),
                            metric_name: float(row.get("value", 0.0)),
                        })
                else:
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
                    if not df.empty and {"project", "department"}.issubset(df.columns):
                        df = df.groupby(["project", "department"], as_index=False).last()

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
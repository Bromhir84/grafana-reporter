import requests, time, copy
from ..config import GRAFANA_URL, HEADERS
from .prometheus_utils import extract_grafana_vars

def filter_panels(panels, excluded_titles_lower):
    filtered = []
    for panel in panels:
        new_panel = copy.deepcopy(panel)
        if "panels" in panel:
            new_panel["panels"] = filter_panels(panel["panels"], excluded_titles_lower)
        title = panel.get("title", "").strip().lower()
        if title not in excluded_titles_lower:
            filtered.append(new_panel)
    return filtered

def clone_dashboard_without_panels(dashboard_uid: str, excluded_titles_lower):
    url = f"{GRAFANA_URL}/api/dashboards/uid/{dashboard_uid}"
    r = requests.get(url, headers=HEADERS)
    r.raise_for_status()
    dash = r.json()["dashboard"]

    GRAFANA_VARS = extract_grafana_vars(dash)
    dash["panels"] = filter_panels(dash.get("panels", []), excluded_titles_lower)

    table_panels = []
    def walk_panels(panels):
        for panel in panels:
            if panel.get("type") == "table":
                exprs = [t["expr"] for t in panel.get("targets", []) if "expr" in t]
                table_panels.append({"title": panel.get("title"), "queries": exprs})
            if "panels" in panel:
                walk_panels(panel["panels"])
    walk_panels(dash.get("panels", []))

    temp_uid = f"{dashboard_uid}-temp-{int(time.time())}"
    dash["uid"] = temp_uid
    dash["title"] = f"{dash['title']} (Temp Copy)"

    payload = {"dashboard": dash, "overwrite": True}
    put_url = f"{GRAFANA_URL}/api/dashboards/db"
    r = requests.post(put_url, headers=HEADERS, json=payload)
    r.raise_for_status()

    return temp_uid, table_panels, GRAFANA_VARS

def delete_dashboard(uid):
    url = f"{GRAFANA_URL}/api/dashboards/uid/{uid}"
    try:
        r = requests.delete(url, headers=HEADERS)
        r.raise_for_status()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Failed to delete dashboard {uid}: {e}")

def extract_uid_from_url(url: str) -> str:
    match = re.search(r"/d/([^/]+)/", url)
    if match:
        return match.group(1)
    raise ValueError("Invalid dashboard URL format. Expected /d/<uid>/")

def paginate_to_a4(img: Image.Image):
    pages = []
    y_offset = 0
    while y_offset < img.height:
        page = Image.new("RGB", (A4_WIDTH_PX, A4_HEIGHT_PX), A4_BG_COLOR)
        crop = img.crop((0, y_offset, A4_WIDTH_PX, min(y_offset + A4_HEIGHT_PX, img.height)))
        page.paste(crop, (0, 0))
        buf = io.BytesIO()
        page.save(buf, format="JPEG", quality=95)
        pages.append(buf.getvalue())
        y_offset += A4_HEIGHT_PX
    return pages

def generate_pdf_from_pages(pages, output_path):
    with open(output_path, "wb") as f:
        f.write(img2pdf.convert(pages))
    logger.info(f"PDF saved to {output_path}")

def resolve_grafana_vars(query: str, variables: dict, start: datetime, end: datetime) -> str:
    """Replace Grafana template variables with Prometheus-compatible values."""
    for var, value in variables.items():
        # Convert Grafana's $__all into regex match-all
        if not value or value in ("$__all", "['$__all']"):
            value = ".*"
        query = query.replace(f"${var}", value)
        query = query.replace(f"${{{var}}}", value)
    
    # Replace $__range with the correct duration
    query = query.replace("$__range", compute_prometheus_duration(start, end))
    
    return query

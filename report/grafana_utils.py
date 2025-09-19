import requests
import time
import logging
from PIL import Image
from ..config import GRAFANA_URL, GRAFANA_API_KEY, A4_WIDTH_PX, A4_HEIGHT_PX, A4_BG_COLOR

logger = logging.getLogger(__name__)
headers = {"Authorization": f"Bearer {GRAFANA_API_KEY}"}


def extract_grafana_vars(dashboard_json):
    vars_dict = {}
    for v in dashboard_json.get("templating", {}).get("list", []):
        value = v.get("current", {}).get("value", ".*")
        vars_dict[v["name"]] = str(value)
    return vars_dict


def filter_panels(panels, excluded_titles_lower):
    filtered = []
    for panel in panels:
        new_panel = panel.copy()
        if "panels" in panel:
            new_panel["panels"] = filter_panels(panel["panels"], excluded_titles_lower)
        title = panel.get("title", "").strip().lower()
        if title not in excluded_titles_lower:
            filtered.append(new_panel)
    return filtered


def clone_dashboard_without_panels(dashboard_uid: str, excluded_titles=None, return_json=False):
    excluded_titles = excluded_titles or []
    excluded_titles_lower = [t.strip().lower() for t in excluded_titles]

    url = f"{GRAFANA_URL}/api/dashboards/uid/{dashboard_uid}"
    r = requests.get(url, headers=headers)
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
    r = requests.post(put_url, headers=headers, json=payload)
    r.raise_for_status()
    if return_json:
        return temp_uid, table_panels, GRAFANA_VARS, dash
    return temp_uid, table_panels, GRAFANA_VARS


def delete_dashboard(uid):
    url = f"{GRAFANA_URL}/api/dashboards/uid/{uid}"
    r = requests.delete(url, headers=headers)
    if r.status_code == 200:
        logger.info(f"Deleted temporary dashboard {uid}")
    else:
        logger.warning(f"Could not delete dashboard {uid}: {r.text}")


def paginate_to_a4(img: Image.Image):
    pages = []
    y_offset = 0
    while y_offset < img.height:
        page = Image.new("RGB", (A4_WIDTH_PX, A4_HEIGHT_PX), A4_BG_COLOR)
        crop = img.crop((0, y_offset, A4_WIDTH_PX, min(y_offset + A4_HEIGHT_PX, img.height)))
        page.paste(crop, (0, 0))
        import io
        buf = io.BytesIO()
        page.save(buf, format="JPEG", quality=95)
        pages.append(buf.getvalue())
        y_offset += A4_HEIGHT_PX
    return pages


def generate_pdf_from_pages(pages, output_path):
    import img2pdf
    with open(output_path, "wb") as f:
        f.write(img2pdf.convert(pages))
    logger.info(f"PDF saved to {output_path}")

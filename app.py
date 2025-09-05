"""
Streamlit Meter Dashboard with MongoDB (GridFS for images)
---------------------------------------------------------
Features:
- Add new meter records (meter_id, consumer_id, value, image)
- Delete meters (also removes image from GridFS)
- Store images in MongoDB GridFS; store metadata in `meters` collection
- Browse meters with search, filters, and pagination
- View images inline; download PDF containing the full meter box
- Edit numeric value in-place (lightweight update)
- Top metrics: Total meters (all) & Matching meters (filtered)
- Case-insensitive IDs (mtr1 == MTR1) and IDs stored/displayed in UPPERCASE
- Clean placeholders in sidebar form
"""

import io
import math
import os
import tempfile
from datetime import datetime
from typing import Optional

import gridfs
from bson import ObjectId
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError
import streamlit as st

# -------------------------
# Helpers
# -------------------------
def norm(s: str) -> str:
    """Normalize IDs for case-insensitive uniqueness & display."""
    return (s or "").strip().upper()

# -------------------------
# Mongo connection
# -------------------------
@st.cache_resource(show_spinner=False)
def get_db():
    uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017/")
    db_name = os.getenv("MONGODB_DB", "meters_db")
    client = MongoClient(uri)
    return client[db_name]

def get_fs(db):
    return gridfs.GridFS(db)

# -------------------------
# Data access layer
# -------------------------
def ensure_indexes(db):
    """
    Uniqueness on normalized fields so IDs are case-insensitive.
    Keeping older exact-case indexes is fine, but the *_norm ones are what we use.
    """
    db.meters.create_index([("meter_id_norm", ASCENDING)], unique=True, name="uniq_meter_norm")
    db.meters.create_index([("consumer_id_norm", ASCENDING)], unique=True, name="uniq_consumer_norm")
    # Legacy indexes (safe if they already exist)
    db.meters.create_index([("meter_id", ASCENDING)], unique=True, name="uniq_meter_id")
    db.meters.create_index([("consumer_id", ASCENDING)], unique=True, name="uniq_consumer")
    db.meters.create_index([("created_at", DESCENDING)])

def save_image(fs: gridfs.GridFS, image_file) -> ObjectId:
    return fs.put(image_file.getvalue(), filename=image_file.name, contentType=image_file.type)

def get_image_bytes(fs: gridfs.GridFS, file_id: ObjectId) -> Optional[bytes]:
    try:
        return fs.get(file_id).read()
    except Exception:
        return None

def insert_meter(db, fs, meter_id: str, consumer_id: str, value: float, image_file) -> ObjectId:
    # Mandatory checks
    missing = []
    if not (meter_id and meter_id.strip()): missing.append("Meter ID")
    if not (consumer_id and consumer_id.strip()): missing.append("Consumer ID")
    if value is None: missing.append("Value")
    if not image_file: missing.append("Image")
    if missing:
        raise ValueError(f"Missing field(s): {', '.join(missing)}")

    # Normalize (UPPERCASE) for both storage and uniqueness
    meter_id_n = norm(meter_id)
    consumer_id_n = norm(consumer_id)

    # Duplicate pre-check (case-insensitive)
    hit = db.meters.find_one(
        {"$or": [{"meter_id_norm": meter_id_n}, {"consumer_id_norm": consumer_id_n}]},
        {"meter_id_norm": 1, "consumer_id_norm": 1}
    )
    if hit:
        if hit.get("meter_id_norm") == meter_id_n and hit.get("consumer_id_norm") == consumer_id_n:
            raise ValueError("Duplicate not allowed: this Meter ID AND Consumer ID already exist (case-insensitive).")
        if hit.get("meter_id_norm") == meter_id_n:
            raise ValueError("Duplicate not allowed: this Meter ID already exists (case-insensitive).")
        if hit.get("consumer_id_norm") == consumer_id_n:
            raise ValueError("Duplicate not allowed: this Consumer ID already exists (case-insensitive).")

    img_id = save_image(fs, image_file)
    doc = {
        # Store display fields in UPPERCASE so cards show uppercase
        "meter_id": meter_id_n,
        "consumer_id": consumer_id_n,
        # Normalized fields used by unique indexes
        "meter_id_norm": meter_id_n,
        "consumer_id_norm": consumer_id_n,
        "value": float(value),
        "image_file_id": img_id,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }
    try:
        res = db.meters.insert_one(doc)
    except DuplicateKeyError as e:
        msg = str(e)
        if "uniq_meter_norm" in msg:
            raise ValueError("Duplicate not allowed: this Meter ID already exists (case-insensitive).")
        if "uniq_consumer_norm" in msg:
            raise ValueError("Duplicate not allowed: this Consumer ID already exists (case-insensitive).")
        if "uniq_meter_id" in msg:
            raise ValueError("Duplicate not allowed: this exact Meter ID already exists.")
        if "uniq_consumer" in msg:
            raise ValueError("Duplicate not allowed: this exact Consumer ID already exists.")
        raise ValueError("Duplicate not allowed: a meter already exists with the same ID(s).")
    return res.inserted_id

def update_value(db, _id: ObjectId, new_value: float):
    db.meters.update_one({"_id": _id}, {"$set": {"value": float(new_value), "updated_at": datetime.utcnow()}})

def delete_meter(db, fs, _id: ObjectId) -> bool:
    doc = db.meters.find_one({"_id": _id})
    if not doc: return False
    img_id = doc.get("image_file_id")
    if img_id:
        try: fs.delete(img_id)
        except Exception: pass
    return db.meters.delete_one({"_id": _id}).deleted_count == 1

def query_meters(db, q: str, consumer_filter: str, sort_by: str, sort_dir: str, page: int, page_size: int):
    filters = {}
    if q:
        filters["$or"] = [
            {"meter_id": {"$regex": q, "$options": "i"}},
            {"consumer_id": {"$regex": q, "$options": "i"}},
        ]
    if consumer_filter:
        filters["consumer_id"] = {"$regex": f"^{consumer_filter}$", "$options": "i"}

    sort_field = {
        "Created": ("created_at", DESCENDING),
        "Meter ID": ("meter_id", ASCENDING),
        "Consumer ID": ("consumer_id", ASCENDING),
        "Value": ("value", DESCENDING),
    }[sort_by]
    direction = DESCENDING if sort_dir == "↓" else ASCENDING

    total_matching = db.meters.count_documents(filters)
    cursor = db.meters.find(filters).sort([(sort_field[0], direction)]).skip(page * page_size).limit(page_size)
    return list(cursor), total_matching

# -------------------------
# PDF generator (ReportLab → FPDF fallback)
# -------------------------
def build_meter_pdf(doc: dict, img_bytes: Optional[bytes]) -> bytes:
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        from reportlab.lib.units import mm
        from reportlab.lib.utils import ImageReader
        from reportlab.lib import colors

        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=A4)
        W, H = A4
        margin = 18 * mm
        x = margin
        y = H - margin

        box_w = W - 2 * margin
        box_h = H - 2 * margin
        c.setLineWidth(1.2)
        c.setStrokeColor(colors.HexColor("#444444"))
        c.roundRect(x, y - box_h, box_w, box_h, 8 * mm, stroke=1, fill=0)

        c.setFont("Helvetica-Bold", 16)
        c.drawString(x + 10, y - 25, "Meter Details")

        c.setFont("Helvetica", 11)
        line_y = y - 50
        line_gap = 16
        def draw_kv(label, value):
            nonlocal line_y
            c.setFont("Helvetica-Bold", 11)
            c.drawString(x + 12, line_y, f"{label}:")
            c.setFont("Helvetica", 11)
            c.drawString(x + 120, line_y, f"{value}")
            line_y -= line_gap

        created = doc.get("created_at")
        updated = doc.get("updated_at")
        created_s = created.strftime("%Y-%m-%d %H:%M UTC") if created else "-"
        updated_s = updated.strftime("%Y-%m-%d %H:%M UTC") if updated else "-"

        draw_kv("Meter ID", doc.get("meter_id", "-"))
        draw_kv("Consumer ID", doc.get("consumer_id", "-"))
        draw_kv("Value", doc.get("value", "-"))
        draw_kv("Created", created_s)
        draw_kv("Updated", updated_s)

        if img_bytes:
            try:
                img = ImageReader(io.BytesIO(img_bytes))
                img_top = line_y - 10
                max_w = box_w - 24
                max_h = img_top - (y - box_h) - 20
                iw, ih = img.getSize()
                ratio = min(max_w / iw, max_h / ih)
                disp_w = iw * ratio
                disp_h = ih * ratio
                img_x = x + (box_w - disp_w) / 2
                img_y = (y - box_h) + 20
                c.drawImage(img, img_x, img_y, width=disp_w, height=disp_h, preserveAspectRatio=True, mask='auto')
            except Exception:
                pass

        c.showPage(); c.save()
        return buf.getvalue()
    except Exception:
        try:
            from fpdf import FPDF
            pdf = FPDF(orientation="P", unit="mm", format="A4")
            pdf.add_page()
            pdf.set_font("Arial", "B", 16); pdf.cell(0, 10, "Meter Details", ln=1)
            pdf.set_font("Arial", "", 12)
            def kv(label, value):
                pdf.set_font("Arial", "B", 12); pdf.cell(40, 8, f"{label}:", ln=0)
                pdf.set_font("Arial", "", 12); pdf.cell(0, 8, f"{value}", ln=1)

            created = doc.get("created_at"); updated = doc.get("updated_at")
            created_s = created.strftime("%Y-%m-%d %H:%M UTC") if created else "-"
            updated_s = updated.strftime("%Y-%m-%d %H:%M UTC") if updated else "-"
            kv("Meter ID", doc.get("meter_id", "-"))
            kv("Consumer ID", doc.get("consumer_id", "-"))
            kv("Value", doc.get("value", "-"))
            kv("Created", created_s); kv("Updated", updated_s)

            if img_bytes:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
                    tmp.write(img_bytes); tmp.flush()
                    pdf.image(tmp.name, x=10, y=None, w=190)
            return pdf.output(dest="S").encode("latin1")
        except Exception as e:
            raise RuntimeError(f"PDF generation requires 'reportlab' or 'fpdf2' package. Error: {e}")

# -------------------------
# UI Components
# -------------------------
def meter_card(doc, fs, db):
    col1, col2 = st.columns([1, 2], vertical_alignment="center")

    with col1:
        img_bytes = None
        if doc.get("image_file_id"):
            img_bytes = get_image_bytes(fs, doc["image_file_id"])
            if img_bytes:
                st.image(img_bytes, caption=f"Meter {doc['meter_id']}", width="stretch")
            else:
                st.info("No image available")
        else:
            st.info("No image uploaded")

    with col2:
        st.markdown(f"**Meter ID:** {doc.get('meter_id','-')}")
        st.markdown(f"**Consumer ID:** {doc.get('consumer_id','-')}")
        st.markdown(f"**Value:** {doc.get('value','-')}")
        st.caption(
            f"Created: {doc.get('created_at').strftime('%Y-%m-%d %H:%M UTC')} • "
            f"Updated: {doc.get('updated_at').strftime('%Y-%m-%d %H:%M UTC')}"
        )

        cA, cB = st.columns([1, 1])
        with cA:
            with st.expander("Update value"):
                new_val = st.number_input(
                    "New reading",
                    value=float(doc.get("value", 0.0)),
                    key=f"val_{doc['_id']}",
                    placeholder="e.g., 0",
                )
                if st.button("Save", key=f"save_{doc['_id']}"):
                    update_value(db, doc["_id"], new_val)
                    st.success("Value updated."); st.rerun()

        with cB:
            with st.expander("Delete meter"):
                st.warning("This will permanently remove this meter and its image.")
                if st.button("Yes, delete", key=f"del_{doc['_id']}"):
                    if delete_meter(db, fs, doc["_id"]): st.success("Deleted."); st.rerun()
                    else: st.error("Delete failed or meter not found.")

        try:
            pdf_bytes = build_meter_pdf(doc, img_bytes)
            st.download_button(
                "Download meter as PDF",
                data=pdf_bytes,
                file_name=f"meter_{doc.get('meter_id','unknown')}.pdf",
                mime="application/pdf",
                use_container_width=True,
                key=f"pdf_{doc['_id']}",
            )
        except Exception as e:
            st.error(f"Could not generate PDF: {e}")

# -------------------------
# Streamlit App
# -------------------------
st.set_page_config(page_title="Meter Dashboard", layout="wide")
st.title("📟 Meter Dashboard")

# Optional CSS to hide the “Press Enter to submit form” hint if your Streamlit shows it
st.markdown("""
<style>
div[data-testid="stNumberInput"] [title="Press Enter to submit form"]{display:none !important;}
</style>
""", unsafe_allow_html=True)

# Connect DB once
db = get_db(); fs = get_fs(db); ensure_indexes(db)

# Sidebar: Add record
with st.sidebar:
    st.header("Add Meter Record")
    with st.form("add_form", clear_on_submit=True):
        meter_id = st.text_input("Meter ID", placeholder="e.g., MTR-001")
        consumer_id = st.text_input("Consumer ID", placeholder="e.g., CSM-1001")
        # Clean placeholder; validate as number
        value_str = st.text_input("Value (reading)", placeholder="e.g., 0")
        image_file = st.file_uploader("Meter Image", type=["png", "jpg", "jpeg"])
        submitted = st.form_submit_button("Save")

        if submitted:
            try:
                value = float(value_str)
                if not meter_id.strip(): raise ValueError("Meter ID is missing.")
                if not consumer_id.strip(): raise ValueError("Consumer ID is missing.")
                if not image_file: raise ValueError("Image is missing.")
                _id = insert_meter(db, fs, meter_id, consumer_id, value, image_file)
                st.success(f"Saved ✓ (id: {_id})"); st.rerun()
            except ValueError as ve:
                st.error(str(ve))
            except DuplicateKeyError as de:
                msg = str(de)
                if "uniq_meter_norm" in msg:
                    st.error("Duplicate not allowed: Meter ID already exists")
                elif "uniq_consumer_norm" in msg:
                    st.error("Duplicate not allowed: Consumer ID already exists")
                elif "uniq_meter_id" in msg:
                    st.error("Duplicate not allowed: exact Meter ID already exists.")
                elif "uniq_consumer" in msg:
                    st.error("Duplicate not allowed: exact Consumer ID already exists.")
                else:
                    st.error("Duplicate not allowed: same ID(s) exist.")
            except Exception as e:
                st.error(f"Failed to save: {e}")

    st.divider()
    st.caption("Environment")
    st.code(
        f"DB: {os.getenv('MONGODB_DB', 'meters_db')}\\n"
        f"URI: {os.getenv('MONGODB_URI', 'mongodb://localhost:27017/')[:35]}…",
        language="bash",
    )

# -------- Top metrics --------
st.subheader("Overview")
colA, colB = st.columns(2)
with colA:
    total_all = db.meters.count_documents({})
    st.metric("Total meters (all)", total_all)

# Main: Filters and list
st.subheader("All Meters")
q = st.text_input("Search (meter/consumer id)")
consumer_filter = st.text_input("Filter by Consumer ID (exact)")

c1, c2, c3, c4 = st.columns([1, 1, 1, 2])
with c1:
    sort_by = st.selectbox("Sort by", ["Created", "Meter ID", "Consumer ID", "Value"], index=0)
with c2:
    sort_dir = st.selectbox("Direction", ["↓", "↑"], index=0)
with c3:
    page_size = st.selectbox("Per page", [6, 9, 12, 24], index=1)
with c4:
    page = st.number_input("Page # (0-based)", min_value=0, step=1)

results, total_matching = query_meters(db, q, consumer_filter, sort_by, sort_dir, int(page), int(page_size))
pages = math.ceil(total_matching / page_size) if page_size else 1

with colB:
    st.metric("Matching meters (filtered)", total_matching)

st.caption(f"Total matching: {total_matching} • Pages: {pages}")

# Grid display
for doc in results:
    with st.container(border=True):
        meter_card(doc, fs, db)

st.divider()
st.caption(
    "Tip: Use the sidebar to add meters. Use search to quickly locate a meter by ID. "
    "Use the Delete panel on a card to remove it."
)

import os, streamlit as st
st.set_page_config(page_title="BD Day – Safe Loader", page_icon="🧪", layout="wide")
st.title("🧪 BD Day – Safe Loader")
st.write("UI is up. Now we'll try to read secrets and show any config errors clearly.")
sheet_url = os.environ.get("SHEET_URL") or getattr(st.secrets, "SHEET_URL", "")
st.info("SHEET_URL detected (masked): " + (sheet_url[:40] + "..." if sheet_url else "NONE"))
st.write("Service account present:", hasattr(st, "secrets") and "gcp_service_account" in st.secrets)
st.success("If you see this, the app is actually running.")

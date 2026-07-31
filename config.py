"""Configuration constants for the rice price scraper."""

import os

# ==========================================
# PIHPS (Bank Indonesia) target configuration
# ==========================================
SOURCE_URL = "https://www.bi.go.id/hargapangan/TabelHarga/PasarTradisionalDaerah"

COMMODITY = "Beras"
PROVINCE = "Jawa Barat"
REGENCY_CITY = "Kota Tasikmalaya"

# Maps the rice type name exactly as shown on the PIHPS site to the
# corresponding `rice_types.id` value in Supabase.
RICE_TYPE_MAP = {
    "Beras Kualitas Bawah I": 1,
    "Beras Kualitas Bawah II": 2,
    "Beras Kualitas Medium I": 3,
    "Beras Kualitas Medium II": 4,
    "Beras Kualitas Super I": 5,
    "Beras Kualitas Super II": 6,
}

# ==========================================
# Supabase configuration
# ==========================================
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

TABLE_RICE_PRICES = "rice_prices"

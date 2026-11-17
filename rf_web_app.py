import io
import math
import re
import zipfile
import xml.sax.saxutils as sax
import polars as pl
import streamlit as st

st.set_page_config(page_title="RF Planning Toolkit", page_icon="📡", layout="wide")


def parse_coordinate(val, is_lat=True):
    if val is None:
        return None
    text = str(val).strip().upper()
    if not text or text in {"NAN", "NONE", "NULL", "-"}:
        return None
    multiplier = -1.0 if any(h in text for h in ["S", "W"]) else 1.0
    cleaned = text.replace(";", ".").replace(",", ".")
    parts = re.findall(r"[-+]?\d+(?:\.\d+)?", cleaned)
    if not parts:
        return None
    try:
        if len(parts) == 1:
            num = float(parts[0])
        elif len(parts) == 2:
            deg = abs(float(parts[0]))
            mins = float(parts[1])
            sign = -1.0 if float(parts[0]) < 0 else 1.0
            num = sign * (deg + mins / 60.0)
        else:
            deg = abs(float(parts[0]))
            mins = float(parts[1])
            secs = float(parts[2])
            sign = -1.0 if float(parts[0]) < 0 else 1.0
            num = sign * (deg + mins / 60.0 + secs / 3600.0)
        num *= multiplier
    except ValueError:
        return None
    if num == 0.0:
        return None
    if is_lat and not (-90.0 <= num <= 90.0):
        return None
    if not is_lat and not (-180.0 <= num <= 180.0):
        return None
    return round(num, 6)


def compute_sector_polygon(lat, lon, azimuth, beamwidth=65.0, radius_meters=350.0, steps=12):
    earth_radius = 6378137.0
    lat_rad = math.radians(lat)
    lon_rad = math.radians(lon)
    angular_dist = radius_meters / earth_radius
    start_angle = azimuth - (beamwidth / 2.0)
    end_angle = azimuth + (beamwidth / 2.0)
    coords = [f"{lon},{lat},0"]
    for i in range(steps + 1):
        bearing = math.radians(start_angle + (end_angle - start_angle) * (i / steps))
        dest_lat = math.asin(
            math.sin(lat_rad) * math.cos(angular_dist)
            + math.cos(lat_rad) * math.sin(angular_dist) * math.cos(bearing)
        )
        dest_lon = lon_rad + math.atan2(
            math.sin(bearing) * math.sin(angular_dist) * math.cos(lat_rad),
            math.cos(angular_dist) - math.sin(lat_rad) * math.sin(dest_lat),
        )
        coords.append(f"{round(math.degrees(dest_lon), 6)},{round(math.degrees(dest_lat), 6)},0")
    coords.append(f"{lon},{lat},0")
    return " ".join(coords)


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def compute_bearing(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return round((math.degrees(math.atan2(x, y)) + 360) % 360, 1)


def auto_idx(cols, keywords, default=0):
    for i, c in enumerate(cols):
        if any(k in c.lower() for k in keywords):
            return i
    return default


st.title("📡 RF Planning Engineering Toolkit")
st.caption("High-speed coordinate cleaner, Google Earth KMZ generator, PCI clash checker, and neighbor analyzer.")

uploaded_file = st.file_uploader("Upload RF Site Dataset (.xlsx or .csv)", type=["xlsx", "xls", "csv"])

if uploaded_file is not None:
    file_bytes = uploaded_file.getvalue()
    if uploaded_file.name.lower().endswith(".csv"):
        df = pl.read_csv(io.BytesIO(file_bytes), infer_schema_length=10000)
    else:
        df = pl.read_excel(io.BytesIO(file_bytes), engine="calamine")

    st.success(f"Loaded **{uploaded_file.name}** — **{df.height:,}** rows × **{df.width}** columns")
    cols = df.columns
    opt_cols = ["None"] + cols

    with st.expander("1. Core Column Mapping (Site ID, Latitude, Longitude)", expanded=True):
        c1, c2, c3 = st.columns(3)
        site_col = c1.selectbox("Site Name / Cell ID", cols, index=auto_idx(cols, ["site", "cell", "name", "id"]))
        lat_col = c2.selectbox("Latitude Column", cols, index=auto_idx(cols, ["lat"]))
        lon_col = c3.selectbox("Longitude Column", cols, index=auto_idx(cols, ["lon", "lng", "long"]))

    tab1, tab2, tab3 = st.tabs(["🗺️ KMZ Map Generator", "⚠️ PCI / EARFCN Clash Check", "📏 Neighbor Distance & Bearing"])

    with tab1:
        col_a, col_b, col_c = st.columns(3)
        sort_col = col_a.selectbox("Sort Sites By", cols, index=cols.index(site_col))
        group_col = col_b.selectbox("Group Into Sub-Folders", opt_cols, index=0)
        color_col = col_c.selectbox("Color-Code By (Band/Status)", opt_cols, index=0)

        draw_sectors = st.checkbox("Draw RF Sector Wedges (Petals) instead of Point Pins", value=False)
        az_col, bw_col, def_bw, rad_m = "None", "None", 65.0, 350.0
        if draw_sectors:
            s1, s2, s3, s4 = st.columns(4)
            az_col = s1.selectbox("Azimuth Column", opt_cols, index=auto_idx(opt_cols, ["azim", "dir", "bearing"]))
            bw_col = s2.selectbox("Beamwidth Column", opt_cols, index=auto_idx(opt_cols, ["beam", "bw"]))
            def_bw = s3.number_input("Default Beamwidth (°)", value=65.0, min_value=5.0, max_value=360.0)
            rad_m = s4.number_input("Sector Radius (m)", value=350.0, min_value=20.0, max_value=10000.0)

        if st.button("Generate Google Earth KMZ", type="primary"):
            with st.spinner("Cleaning coordinates, sorting, and building compressed KMZ..."):
                df_clean = df.with_columns([
                    pl.col(lat_col).map_elements(lambda x: parse_coordinate(x, True), return_dtype=pl.Float64).alias("_lat"),
                    pl.col(lon_col).map_elements(lambda x: parse_coordinate(x, False), return_dtype=pl.Float64).alias("_lon"),
                ])
                valid_df = df_clean.filter(pl.col("_lat").is_not_null() & pl.col("_lon").is_not_null())
                dropped = df.height - valid_df.height

                if valid_df.height == 0:
                    st.error("No valid coordinates found after cleaning.")
                else:
                    sort_list = [group_col, sort_col] if group_col != "None" else [sort_col]
                    valid_df = valid_df.sort(sort_list)
                    first_lat = valid_df["_lat"][0]
                    first_lon = valid_df["_lon"][0]
                    first_site = sax.escape(str(valid_df[site_col][0]))
                    popup_cols = [c for c in valid_df.columns if not c.startswith("_")][:12]

                    palette = [
                        "ffffaa00", "ff0055ff", "ff00cc44", "ffaa00ff",
                        "ff00ffff", "ffff00aa", "ff3388ff", "ff88ff00"
                    ]
                    color_map = {}
                    if color_col != "None":
                        uniq_vals = valid_df[color_col].drop_nulls().unique().to_list()
                        for idx, val in enumerate(uniq_vals):
                            color_map[str(val)] = palette[idx % len(palette)]

                    kml_parts = [
                        '<?xml version="1.0" encoding="UTF-8"?>',
                        '<kml xmlns="http://www.opengis.net/kml/2.2">',
                        '<Document>',
                        '  <name>RF_Network_Plan.kmz</name>',
                        '  <open>0</open>',
                        f'  <LookAt><longitude>{first_lon}</longitude><latitude>{first_lat}</latitude>'
                        '<altitude>0</altitude><heading>0</heading><tilt>0</tilt><range>5000</range></LookAt>',
                    ]

                    for idx, hex_col in enumerate(palette):
                        poly_col = "66" + hex_col[2:]
                        kml_parts.append(
                            f'  <Style id="style_{idx}">'
                            f'<IconStyle><color>{hex_col}</color><scale>0.7</scale>'
                            '<Icon><href>http://maps.google.com/mapfiles/kml/shapes/placemark_circle.png</href></Icon></IconStyle>'
                            '<LabelStyle><scale>0.7</scale></LabelStyle>'
                            f'<LineStyle><color>{hex_col}</color><width>1.5</width></LineStyle>'
                            f'<PolyStyle><color>{poly_col}</color></PolyStyle>'
                            '</Style>'
                        )

                    needed_cols = {site_col, "_lat", "_lon"} | set(popup_cols)
                    if group_col != "None":
                        needed_cols.add(group_col)
                    if color_col != "None":
                        needed_cols.add(color_col)
                    if draw_sectors and az_col != "None":
                        needed_cols.add(az_col)
                        if bw_col != "None":
                            needed_cols.add(bw_col)

                    if group_col != "None":
                        partitions = valid_df.select(list(needed_cols)).partition_by(group_col, as_dict=True)
                    else:
                        partitions = {("All RF Sites",): valid_df.select(list(needed_cols))}

                    for group_key, sub_df in partitions.items():
                        raw_label = group_key[0] if isinstance(group_key, tuple) else group_key
                        folder_label = sax.escape(str(raw_label)) if raw_label is not None else "Uncategorized"
                        kml_parts.extend([
                            '  <Folder>',
                            f'    <name>{folder_label} ({sub_df.height:,})</name>',
                            '    <visibility>0</visibility>',
                            '    <open>0</open>',
                        ])

                        for row in sub_df.to_dicts():
                            name = sax.escape(str(row[site_col]))
                            lat, lon = row["_lat"], row["_lon"]
                            style_idx = 0
                            if color_col != "None" and row.get(color_col) is not None:
                                c_hex = color_map.get(str(row[color_col]), palette[0])
                                style_idx = palette.index(c_hex)

                            desc_rows = "".join(
                                f"<tr><td><b>{sax.escape(str(k))}</b></td><td>{sax.escape(str(row[k]))}</td></tr>"
                                for k in popup_cols if k in row and row[k] is not None
                            )
                            desc_html = f"<![CDATA[<table border='1' cellpadding='3'>{desc_rows}</table>]]>"

                            drew_sector = False
                            if draw_sectors and az_col != "None" and row.get(az_col) is not None:
                                try:
                                    az = float(str(row[az_col]).strip())
                                    bw = float(row[bw_col]) if (bw_col != "None" and row.get(bw_col) is not None) else def_bw
                                    poly_coords = compute_sector_polygon(lat, lon, az, bw, rad_m)
                                    kml_parts.append(
                                        f'    <Placemark><name>{name}</name><visibility>0</visibility>'
                                        f'<styleUrl>#style_{style_idx}</styleUrl><description>{desc_html}</description>'
                                        f'<Polygon><outerBoundaryIs><LinearRing><coordinates>{poly_coords}</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>'
                                    )
                                    drew_sector = True
                                except ValueError:
                                    drew_sector = False

                            if not drew_sector:
                                kml_parts.append(
                                    f'    <Placemark><name>{name}</name><visibility>0</visibility>'
                                    f'<styleUrl>#style_{style_idx}</styleUrl><description>{desc_html}</description>'
                                    f'<Point><coordinates>{lon},{lat},0</coordinates></Point></Placemark>'
                                )

                        kml_parts.append('  </Folder>')

                    kml_parts.extend(['</Document>', '</kml>'])

                    kmz_buf = io.BytesIO()
                    with zipfile.ZipFile(kmz_buf, "w", compression=zipfile.ZIP_DEFLATED) as kmz:
                        kmz.writestr("doc.kml", "\n".join(kml_parts).encode("utf-8"))

                    st.info(
                        f"Exported **{valid_df.height:,}** valid sites (Cleaned **{dropped:,}** invalid rows). "
                        f"Auto-zoom target: **{first_site}** (`{first_lat}, {first_lon}`). Loaded unchecked in Google Earth."
                    )
                    st.download_button(
                        label="⬇️ Download RF_Network_Plan.kmz",
                        data=kmz_buf.getvalue(),
                        file_name="RF_Network_Plan.kmz",
                        mime="application/vnd.google-earth.kmz",
                    )

    with tab2:
        p1, p2, p3 = st.columns(3)
        pci_col = p1.selectbox("PCI Column", cols, index=auto_idx(cols, ["pci"]))
        earfcn_col = p2.selectbox("EARFCN / Frequency Band Column", opt_cols, index=auto_idx(opt_cols, ["earfcn", "freq", "band", "uarfcn"]))
        max_km = p3.number_input("Clash Search Radius (km)", value=5.0, min_value=0.1, max_value=50.0)

        if st.button("Run PCI Clash Audit", type="primary"):
            with st.spinner("Scanning spatial grid for co-channel PCI collisions..."):
                df_c = df.with_columns([
                    pl.col(lat_col).map_elements(lambda x: parse_coordinate(x, True), return_dtype=pl.Float64).alias("_lat"),
                    pl.col(lon_col).map_elements(lambda x: parse_coordinate(x, False), return_dtype=pl.Float64).alias("_lon"),
                ]).filter(pl.col("_lat").is_not_null() & pl.col("_lon").is_not_null() & pl.col(pci_col).is_not_null())

                select_list = [site_col, "_lat", "_lon", pci_col]
                if earfcn_col != "None":
                    select_list.append(earfcn_col)

                rows = df_c.select(select_list).to_dicts()
                cell_deg = max_km / 111.0
                grid = {}
                for idx, r in enumerate(rows):
                    key = (int(r["_lat"] / cell_deg), int(r["_lon"] / cell_deg))
                    grid.setdefault(key, []).append(idx)

                clashes = []
                for idx, r1 in enumerate(rows):
                    gx, gy = int(r1["_lat"] / cell_deg), int(r1["_lon"] / cell_deg)
                    pci1 = str(r1[pci_col]).strip()
                    ch1 = str(r1[earfcn_col]).strip() if earfcn_col != "None" else "ALL"

                    for dx in (-1, 0, 1):
                        for dy in (-1, 0, 1):
                            for j in grid.get((gx + dx, gy + dy), []):
                                if j <= idx:
                                    continue
                                r2 = rows[j]
                                if str(r2[pci_col]).strip() != pci1:
                                    continue
                                ch2 = str(r2[earfcn_col]).strip() if earfcn_col != "None" else "ALL"
                                if ch1 != ch2:
                                    continue
                                dist = haversine_km(r1["_lat"], r1["_lon"], r2["_lat"], r2["_lon"])
                                if dist <= max_km:
                                    sev = "CRITICAL (Co-Site)" if dist < 0.05 else ("HIGH (<2 km)" if dist < 2.0 else "MODERATE")
                                    clashes.append({
                                        "Severity": sev,
                                        "Distance_km": round(dist, 3),
                                        "PCI": pci1,
                                        "Channel_Band": ch1,
                                        "Cell_A": r1[site_col],
                                        "Cell_B": r2[site_col],
                                    })

                if not clashes:
                    st.success(f"Zero PCI clashes found within {max_km} km!")
                else:
                    res_df = pl.DataFrame(clashes).sort("Distance_km")
                    st.warning(f"Found **{res_df.height:,}** PCI clashes within {max_km} km.")
                    st.dataframe(res_df.head(500), use_container_width=True)
                    st.download_button(
                        label="⬇️ Download PCI_Clashes_Report.csv",
                        data=res_df.write_csv().encode("utf-8"),
                        file_name="PCI_Clashes_Report.csv",
                        mime="text/csv",
                    )

    with tab3:
        n1, n2, n3 = st.columns(3)
        top_n = n1.number_input("Nearest Neighbors per Site", value=5, min_value=1, max_value=30)
        max_nbr_km = n2.number_input("Max Neighbor Radius (km)", value=15.0, min_value=0.5, max_value=100.0)
        band_filter = n3.selectbox("Match Same Band/EARFCN Only", opt_cols, index=0)

        if st.button("Calculate Neighbor Distances", type="primary"):
            with st.spinner("Computing inter-site distances and bearings..."):
                df_n = df.with_columns([
                    pl.col(lat_col).map_elements(lambda x: parse_coordinate(x, True), return_dtype=pl.Float64).alias("_lat"),
                    pl.col(lon_col).map_elements(lambda x: parse_coordinate(x, False), return_dtype=pl.Float64).alias("_lon"),
                ]).filter(pl.col("_lat").is_not_null() & pl.col("_lon").is_not_null())

                uniq_cols = [site_col, "_lat", "_lon"] + ([band_filter] if band_filter != "None" else [])
                sites = df_n.select(uniq_cols).unique(subset=[site_col, "_lat", "_lon"]).to_dicts()

                cell_deg = max_nbr_km / 111.0
                grid = {}
                for idx, r in enumerate(sites):
                    key = (int(r["_lat"] / cell_deg), int(r["_lon"] / cell_deg))
                    grid.setdefault(key, []).append(idx)

                nbr_rows = []
                for idx, r1 in enumerate(sites):
                    gx, gy = int(r1["_lat"] / cell_deg), int(r1["_lon"] / cell_deg)
                    b1 = str(r1[band_filter]).strip() if band_filter != "None" else "ALL"
                    candidates = []

                    for dx in (-1, 0, 1):
                        for dy in (-1, 0, 1):
                            for j in grid.get((gx + dx, gy + dy), []):
                                if j == idx:
                                    continue
                                r2 = sites[j]
                                if band_filter != "None" and str(r2[band_filter]).strip() != b1:
                                    continue
                                dist = haversine_km(r1["_lat"], r1["_lon"], r2["_lat"], r2["_lon"])
                                if 0.01 <= dist <= max_nbr_km:
                                    candidates.append((dist, r2))

                    candidates.sort(key=lambda x: x[0])
                    for rank, (dist, r2) in enumerate(candidates[:int(top_n)], start=1):
                        nbr_rows.append({
                            "Source_Site": r1[site_col],
                            "Neighbor_Rank": rank,
                            "Neighbor_Site": r2[site_col],
                            "Distance_km": round(dist, 3),
                            "Bearing_deg": compute_bearing(r1["_lat"], r1["_lon"], r2["_lat"], r2["_lon"]),
                        })

                if not nbr_rows:
                    st.warning("No neighbors found within the selected radius.")
                else:
                    nbr_df = pl.DataFrame(nbr_rows)
                    st.success(f"Generated **{nbr_df.height:,}** neighbor relations.")
                    st.dataframe(nbr_df.head(500), use_container_width=True)
                    st.download_button(
                        label="⬇️ Download Neighbor_Distances.csv",
                        data=nbr_df.write_csv().encode("utf-8"),
                        file_name="Neighbor_Distances.csv",
                        mime="text/csv",
                    )

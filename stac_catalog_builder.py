#!/usr/bin/env python3
"""
STAC Catalog Builder for Raster Datasets

This script builds a STAC-compliant catalog from a raster file archive,
integrating metadata from a PostgreSQL database, and outputs STAC JSON files.
- Raster file bounding boxes are computed using rasterio
- Collections and Items are built from DB metadata and file structure
- All output is written as STAC-compliant JSON

Dependencies:
    pystac, psycopg2, rasterio, numpy, python-dateutil

Configuration:
    - Database credentials are read from db_config.json (see example).
    - Paths and other constants are set via variables at the top of the script.
"""

import os
import csv
import ast
import json
import glob
import time
import psycopg2
import pystac
from pystac import Catalog, Collection, Item, Asset, Extent, SpatialExtent, TemporalExtent, Link
from datetime import datetime, date
import rasterio
from rasterio.warp import transform_bounds
import numpy as np
from dateutil.parser import parse as dateutil_parse
from os.path import getsize
import logging

# --- CONFIGURATION ---

RASTER_ROOT = "/datacloud/raster"  # Root directory for raster datasets
STAC_ROOT = "/home/opentopo/apps/tomcat9/webapps/stac/"  # Output root for STAC JSON files
ITEMS_DIR = os.path.join(STAC_ROOT, "items")  # Directory for STAC item JSON files
INDEX_CSV = os.path.join(STAC_ROOT, "raster_index.csv")  # Raster index CSV file
STAC_INDEX_CSV = os.path.join(STAC_ROOT, "stac_index.csv")  # STAC index CSV file
#BASE_URL = "https://twsa.ucsd.edu/STAC"  # Base public URL for catalog
BASE_URL = "https://portal.opentopography.org/stac"  # Base public URL for catalog
ITEMS_URL = f"{BASE_URL}/items"
DATA_URL_PREFIX = "https://opentopography.s3.sdsc.edu/raster"  # Public prefix for raster data

LICENSE_URLS = {
    "CC BY 4.0": "https://creativecommons.org/licenses/by/4.0/",
    "CC BY-NC 4.0": "https://creativecommons.org/licenses/by-nc/4.0/",
    "CC0 1.0": "https://creativecommons.org/share-your-work/public-domain/cc0/",
    "GNU GPLv3": "https://www.gnu.org/licenses/gpl-3.0.html",
    "Open Government Licence - Canada": "https://open.canada.ca/en/open-government-licence-canada",
    "EOSDIS Data Use Policy": "https://www.earthdata.nasa.gov/learn/use-data/data-use-policy"
}

DB_CONFIG_FILE = "db_config.json"  # Path to database config file

# --- LOGGING CONFIGURATION ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def load_db_config(config_path):
    """
    Loads database connection parameters from a JSON configuration file.
    Returns a dict with keys: host, port, dbname, user, password.
    """
    try:
        with open(config_path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error(f"Database configuration file not found: {config_path}")
        raise
    except json.JSONDecodeError:
        logger.error(f"Error decoding JSON from database configuration file: {config_path}")
        raise

def db_connect():
    """
    Connects to the PostgreSQL database using credentials from db_config.json.
    Returns a psycopg2 connection object.
    """
    try:
        db_conn = load_db_config(DB_CONFIG_FILE)
        return psycopg2.connect(
            host=db_conn['host'],
            port=db_conn['port'],
            dbname=db_conn['dbname'],
            user=db_conn['user'],
            password=db_conn['password']
        )
    except Exception as e:
        logger.error(f"Failed to connect to database: {e}")
        raise

def get_collections_from_db():
    """
    Retrieves dataset (collection) metadata from the database.
    Returns a list of dictionaries for each collection.
    """
    conn = None
    try:
        conn = db_connect()
        cur = conn.cursor()
        cur.execute("""
            SELECT
                d.short_name as shortname,
                d.dataset_name,
                d.description as dataset_description, -- NEW: Added d.description
                d.opentopoid as id,
                d.otcollectionid as collectionid,
                'https://doi.org/' || d.DOI as sci_doi,
                'https://portal.opentopography.org' || d.access_url as url,
                d.start_date,
                d.end_date,
                d.use_license,
                g.geojson,
                g.extent,
                TRIM(d.citation) AS raw_citation, -- For sci:citation
                CASE
                  WHEN TRIM(d.citation) IS NULL OR TRIM(d.citation) = '' THEN
                    'N/A. For citation guidance, please refer to: https://opentopography.org/citations'
                  ELSE
                    REGEXP_REPLACE(TRIM(d.citation), '\.\s*$', '') || '. Accessed <YYYY-MM-DD>. For additional citation guidance, see: https://opentopography.org/citations'
                END AS formatted_citation_text -- This will go into a link title
            FROM
                catalog.dataset_full d
            JOIN
                catalog.dataset_geom_summary g
                ON d.opentopoid = g.rt_opentopoid
            WHERE
                d.product_format = 'Raster'
                AND d.ot_hosted
                AND d.collaborative_id = 0
                AND d.approved
            ORDER BY
                d.short_name
            --LIMIT 5 -- For fast testing, remove or set higher for production
        """)
        cols = [desc[0] for desc in cur.description]
        collections = []
        for row in cur.fetchall():
            collections.append(dict(zip(cols, row)))
        cur.close()
        return collections
    except Exception as e:
        logger.error(f"Error retrieving collections from database: {e}")
        return []
    finally:
        if conn:
            conn.close()

def get_additional_providers(collection_id):
    """
    Get additional provider information for a collection from the database.
    Returns a list of provider dicts.
    """
    conn = None
    try:
        conn = db_connect()
        cur = conn.cursor()
        cur.execute("""
            SELECT
                CASE WHEN is_organization THEN organization ELSE full_name END AS name,
                CASE WHEN is_organization THEN provider_url ELSE 'mailto:' || email END AS url,
                role
            FROM catalog.data_providers
            WHERE otcollectionid = %s
            ORDER BY is_organization DESC, role, name
        """, (collection_id,))
        providers = []
        for row in cur.fetchall():
            name, url, role = row
            providers.append({
                "name": name,
                "url": url,
                "roles": [role] if role else []
            })
        cur.close()
        return providers
    except Exception as e:
        logger.error(f"Error retrieving providers for collection {collection_id}: {e}")
        return []
    finally:
        if conn:
            conn.close()

def parse_extent(extent_box):
    """
    Parses a PostGIS BOX string and returns [minx, miny, maxx, maxy] as floats.
    Returns None if parsing fails.
    """
    if not extent_box:
        return None
    vals = extent_box.replace('BOX(', '').replace(')', '').replace(',', ' ').split()
    if len(vals) != 4:
        return None
    try:
        return [float(vals[0]), float(vals[1]), float(vals[2]), float(vals[3])]
    except Exception:
        logger.warning(f"Could not parse extent box string: {extent_box}")
        return None

def bbox_to_polygon(bbox):
    """
    Converts a bbox [minx, miny, maxx, maxy] to a GeoJSON Polygon geometry dict.
    """
    minx, miny, maxx, maxy = bbox
    return {
        "type": "Polygon",
        "coordinates": [[
            [minx, miny],
            [maxx, miny],
            [maxx, maxy],
            [minx, maxy],
            [minx, miny]
        ]]
    }

def parse_bbox(val):
    """
    Parses a bbox string from the raster index CSV into a list of floats.
    Returns None if parsing fails or values are unreasonable.
    """
    try:
        bbox = ast.literal_eval(val)
        if bbox is None: return None
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
            for x in bbox:
                if not isinstance(x, (int, float)):
                    return None
                # Check for extreme values or NaN (latitude -90 to 90, longitude -180 to 180)
                if abs(x) > 181 or np.isnan(x):
                    return None
            return list(bbox)
        return None
    except Exception:
        logger.warning(f"Could not parse bbox string from index: {val}")
        return None

def load_index(index_csv):
    """
    Loads the raster index CSV into a dictionary: path -> (mtime, bbox)
    """
    index = {}
    try:
        with open(index_csv, newline='') as f:
            reader = csv.DictReader(f)
            for row in reader:
                path = row["path"]
                mtime = float(row["mtime"])
                bbox = parse_bbox(row["bbox"])
                index[path] = (mtime, bbox)
    except FileNotFoundError:
        logger.warning(f"Raster index CSV not found: {index_csv}. It will be built.")
    except Exception as e:
        logger.error(f"Error loading raster index CSV {index_csv}: {e}")
    return index

def build_raster_index(raster_root, index_csv):
    """
    Scans the raster_root directory tree for raster files and writes an index CSV
    with columns: path, mtime, bbox (in EPSG:4326).
    """
    logger.info("Building raster index...")
    exts = ('.tif', '.tiff', '.img', '.adf', '.flt', '.grd')
    scanned_count = 0
    with open(index_csv, "w", newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["path", "mtime", "bbox"])
        for dirpath, dirnames, filenames in os.walk(raster_root):
            dirnames.sort()
            filenames.sort()
            for fname in filenames:
                if fname.lower().endswith(exts):
                    full_path = os.path.join(dirpath, fname)
                    try:
                        mtime = os.path.getmtime(full_path)
                        bbox = None
                        try:
                            with rasterio.open(full_path) as src:
                                if src.crs:
                                    try:
                                        # densify_pts helps with accurate re-projection of bounding boxes
                                        bbox = list(transform_bounds(
                                            src.crs, "EPSG:4326", *src.bounds, densify_pts=21))
                                        # Basic sanity check for geographical coordinates
                                        if not all(isinstance(b, (int, float)) and -181 <= b <= 181 for b in bbox):
                                            logger.warning(f"Nonsense bbox for {full_path}: {bbox}. Skipping bbox.")
                                            bbox = None
                                    except Exception as e:
                                        logger.warning(f"Could not transform bounds for {full_path} ({e}). Skipping bbox.")
                                        bbox = None
                                else:
                                    logger.warning(f"{full_path} has no CRS. Skipping bbox.")
                                    bbox = None
                        except rasterio.errors.RasterioIOError as e:
                            logger.warning(f"Could not open raster file {full_path} ({e}). Skipping bbox for this file.")
                            bbox = None
                        except Exception as e:
                            logger.warning(f"Error processing raster with rasterio {full_path} ({e}). Skipping bbox.")
                            bbox = None
                        writer.writerow([full_path, mtime, repr(bbox)])
                        scanned_count += 1
                    except Exception as e:
                        logger.warning(f"Could not stat {full_path} ({e}). Skipping.")
                        continue
    logger.info(f"Raster index built for {scanned_count} files and written to {index_csv}")

def build_stac_index(stac_root, items_dir, stac_index_csv):
    """
    Scans items_dir for all item JSON files and writes a CSV index with columns:
    id, title, self_href (file path).
    """
    logger.info("Building STAC index...")
    indexed_count = 0
    with open(stac_index_csv, "w", newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["id", "title", "self_href"])
        for item_path in glob.glob(os.path.join(items_dir, "*.json")):
            try:
                with open(item_path) as jf:
                    data = json.load(jf)
                id_ = data.get("id")
                title = data.get("properties", {}).get("title", "")
                writer.writerow([id_, title, item_path])
                indexed_count += 1
            except Exception as e:
                logger.error(f"Error processing STAC item {item_path} for index: {e}")
                continue
    logger.info(f"STAC index built for {indexed_count} items and written to {stac_index_csv}")

def find_layers(dataset_path, dataset_id):
    """
    Yields (layer_name, layer_path) for each subdirectory in dataset_path whose name
    starts with dataset_id (used as a convention for layers).
    """
    # Using os.scandir for potentially better performance
    try:
        for entry in sorted(os.scandir(dataset_path), key=lambda e: e.name):
            layer_path = entry.path
            if entry.is_dir() and entry.name.startswith(dataset_id):
                yield entry.name, layer_path
    except FileNotFoundError:
        logger.warning(f"Layer directory not found: {dataset_path}")
    except Exception as e:
        logger.error(f"Error scanning for layers in {dataset_path}: {e}")


def find_vrt(dataset_path, layer_name):
    """
    Returns the path to the VRT file for a given layer, or None if not found.
    """
    vrt_path = os.path.join(dataset_path, f"{layer_name}.vrt")
    if os.path.isfile(vrt_path):
        return vrt_path
    return None

def find_rasters_from_index(layer_path, index):
    """
    Returns a list of raster file paths from index that are under layer_path.
    """
    rasters = []
    abs_layer_path = os.path.abspath(layer_path)
    # Optimized search: check if path starts with layer_path
    for path in index:
        abs_path = os.path.abspath(path)
        if abs_path.startswith(abs_layer_path + os.sep) or abs_path == abs_layer_path:
            rasters.append(path)
    return rasters

def get_bbox_and_datetime_from_index(raster_path, index):
    """
    For a given raster_path, returns (bbox, datetime) as found in the index.
    If mtime is present, datetime is a UTC datetime.
    """
    mtime, bbox = index.get(raster_path, (None, None))
    dt = datetime.utcfromtimestamp(mtime) if mtime else None
    return bbox, dt

def update_bbox(current_bbox, new_bbox):
    """
    Expands current_bbox to include new_bbox. Returns the updated bbox.
    """
    if current_bbox is None:
        return list(new_bbox) # Create a copy to avoid modification issues
    return [
        min(current_bbox[0], new_bbox[0]),
        min(current_bbox[1], new_bbox[1]),
        max(current_bbox[2], new_bbox[2]),
        max(current_bbox[3], new_bbox[3]),
    ]

def get_asset_bbox_and_geometry_from_index(raster_path, index):
    """
    Returns (bbox, geometry) for the raster asset. Tries the index first,
    else reads from file directly.
    """
    _, bbox = index.get(raster_path, (None, None))
    if bbox and all(isinstance(b, (int, float)) for b in bbox) and all(-181 <= c <= 181 for c in bbox):
        geometry = bbox_to_polygon(bbox)
        return bbox, geometry
    else:
        # Fallback to reading from file if index data is missing or invalid
        try:
            with rasterio.open(raster_path) as src:
                if src.crs:
                    bbox = list(transform_bounds(
                        src.crs, "EPSG:4326", *src.bounds, densify_pts=21))
                    if not all(isinstance(b, (int, float)) and -181 <= b <= 181 for b in bbox):
                        logger.warning(f"Fallback: Nonsense bbox for {raster_path}: {bbox}. Returning None.")
                        return None, None
                    geometry = bbox_to_polygon(bbox)
                    return bbox, geometry
                else:
                    logger.warning(f"Fallback: {raster_path} has no CRS. Returning None.")
                    return None, None
        except rasterio.errors.RasterioIOError as e:
            logger.warning(f"Fallback: Could not open raster file {raster_path} ({e}). Returning None.")
            return None, None
        except Exception as e:
            logger.warning(f"Fallback: Error processing raster {raster_path} ({e}). Returning None.")
            return None, None

def fix_links_in_json_files(stac_root, base_url, items_url, data_url_prefix):
    """
    Rewrites all links in STAC catalog, collections, and item JSONs to use
    public URLs rather than file system paths. This is run after initial saves.
    """
    logger.info("Rewriting links in STAC JSON files to public URLs...")

    # Fix root catalog links
    root_path = os.path.join(stac_root, "ot_raster_collection.json")
    try:
        with open(root_path) as f:
            data = json.load(f)
        for link in data.get("links", []):
            if link.get("rel") == "self":
                link["href"] = f"{base_url}/ot_raster_collection.json"
            elif link.get("rel") in ("root", "parent"):
                link["href"] = f"{base_url}/ot_raster_collection.json"
            elif link.get("rel") == "child":
                fname = os.path.basename(link["href"])
                link["href"] = f"{base_url}/{fname}"
        with open(root_path, "w") as f:
            json.dump(data, f, indent=2)
        logger.debug(f"Fixed links for root catalog: {root_path}")
    except Exception as e:
        logger.error(f"Error fixing links for root catalog {root_path}: {e}")

    # Fix collection links
    for coll_path in glob.glob(os.path.join(stac_root, "*_collection.json")):
        try:
            with open(coll_path) as f:
                data = json.load(f)
            cid = data["id"]
            for link in data.get("links", []):
                if link.get("rel") == "self":
                    link["href"] = f"{base_url}/{cid}_collection.json"
                elif link.get("rel") in ("root", "parent"):
                    link["href"] = f"{base_url}/ot_raster_collection.json"
                elif link.get("rel") == "item":
                    fname = os.path.basename(link["href"])
                    link["href"] = f"{items_url}/{fname}"
            with open(coll_path, "w") as f:
                json.dump(data, f, indent=2)
            logger.debug(f"Fixed links for collection: {coll_path}")
        except Exception as e:
            logger.error(f"Error fixing links for collection {coll_path}: {e}")

    # Fix item links, and update asset URLs to public data URLs
    for item_path in glob.glob(os.path.join(stac_root, "items", "*.json")):
        try:
            with open(item_path) as f:
                data = json.load(f)
            iid = data["id"]
            dataset_id = data.get("collection")
            if not dataset_id and '_' in iid:
                 dataset_id = "_".join(iid.split("_")[:-1])

            for link in data.get("links", []):
                if link.get("rel") == "self":
                    link["href"] = f"{items_url}/{iid}.json"
                elif link.get("rel") == "collection":
                    link["href"] = f"{base_url}/{dataset_id}_collection.json" if dataset_id else f"{base_url}/MISSING_COLLECTION.json"
                elif link.get("rel") == "parent":
                    link["href"] = f"{base_url}/{dataset_id}_collection.json" if dataset_id else f"{base_url}/MISSING_COLLECTION.json"
                elif link.get("rel") == "root":
                    link["href"] = f"{base_url}/ot_raster_collection.json"
            for asset_key, asset_val in data.get("assets", {}).items():
                if asset_val["href"].startswith(RASTER_ROOT):
                    asset_val["href"] = asset_val["href"].replace(RASTER_ROOT, data_url_prefix).replace("\\", "/")
            with open(item_path, "w") as f:
                json.dump(data, f, indent=2)
            logger.debug(f"Fixed links for item: {item_path}")
        except Exception as e:
            logger.error(f"Error fixing links for item {item_path}: {e}")
    logger.info("Finished rewriting links.")


def ensure_datetime(dt):
    """
    Converts a date/datetime or ISO string to a datetime object.
    Returns None if conversion fails.
    """
    if isinstance(dt, datetime):
        return dt
    if isinstance(dt, date):
        return datetime(dt.year, dt.month, dt.day)
    if isinstance(dt, str):
        try:
            dt2 = dateutil_parse(dt)
            if isinstance(dt2, datetime):
                return dt2
            if isinstance(dt2, date):
                return datetime(dt2.year, dt2.month, dt2.day)
        except Exception as e:
            logger.warning(f"Could not parse date string '{dt}': {e}")
    return None

def minify_geometry(geom_dict):
    """
    Returns a minified JSON version of a geometry dictionary.
    This is effectively a no-op when saving with pystac due to default JSON.dumps behavior.
    """
    return json.loads(json.dumps(geom_dict, separators=(',', ':')))

def get_display_layer_name(layer_name, shortname):
    """
    Maps a layer_name to its display name (e.g. adds _DTM or _DSM).
    """
    suffix = layer_name[len(shortname):]
    if suffix == "_be":
        return shortname + "_DTM"
    elif suffix == "_hh":
        return shortname + "_DSM"
    else:
        return layer_name

def main():
    """
    Main entry point for the script.
    Orchestrates the index building, STAC creation, and timing.
    """
    # Record script start time
    script_start = time.time()
    logger.info("STAC Catalog Builder started.")

    # Ensure output directories exist
    os.makedirs(STAC_ROOT, exist_ok=True)
    os.makedirs(ITEMS_DIR, exist_ok=True)

    # --- Timing: Build raster index ---
    raster_index_start = time.time()
    if not os.path.exists(INDEX_CSV):
        build_raster_index(RASTER_ROOT, INDEX_CSV)
    else:
        logger.info(f"Raster index CSV already exists at {INDEX_CSV}. Skipping build.")
    raster_index_end = time.time()
    logger.info(f"build_raster_index runtime: {raster_index_end - raster_index_start:.2f} seconds")

    # --- The rest of the script ---
    logger.info("Continuing with catalog creation...")
    rest_start = time.time()

    index = load_index(INDEX_CSV)
    if not index:
        logger.critical("Raster index is empty or failed to load. Cannot proceed with catalog creation.")
        return

    datasets = get_collections_from_db()
    if not datasets:
        logger.critical("No datasets retrieved from database. Cannot proceed with catalog creation.")
        return

    collection_objs = [] # Stores pystac Collection objects
    root_child_links = [] # Stores pystac Link objects for the root catalog's children

    for ds in datasets:
        shortname = ds.get("shortname")
        datasetname = ds.get("dataset_name")
        dataset_description = ds.get("dataset_description") # NEW: Retrieve the DB description
        opentopoid = ds.get("id")
        otcollectionid = ds.get("collectionid")
        sci_doi = ds.get("sci_doi")
        url = ds.get("url")
        start_date = ds.get("start_date")
        end_date = ds.get("end_date")
        use_license = ds.get("use_license")
        geojson = ds.get("geojson")
        extent_box = ds.get("extent")
        raw_citation = ds.get("raw_citation")
        formatted_citation_text = ds.get("formatted_citation_text")

        if not shortname or not opentopoid or not otcollectionid:
            logger.error(f"Missing essential metadata for a dataset: shortname={shortname}, id={opentopoid}, collectionid={otcollectionid}. Skipping.")
            continue

        dataset_path = os.path.join(RASTER_ROOT, shortname)
        if not os.path.isdir(dataset_path):
            logger.warning(f"Dataset directory for shortname '{shortname}' ({dataset_path}) not found, skipping.")
            continue

        # Use opentopoid for collection filename to ensure unique and consistent naming
        collection_json_filename = f"{opentopoid}_collection.json"
        collection_path = os.path.join(STAC_ROOT, collection_json_filename)

        if os.path.exists(collection_path):
            logger.info(f"Skipping {shortname}: {collection_path} already exists. Delete to re-create.")
            # If collection JSON already exists, load it to add to root_child_links
            try:
                existing_collection = Collection.from_file(collection_path)
                collection_objs.append(existing_collection)
                root_child_links.append(pystac.Link.child(
                    target=existing_collection,
                    title=f"{existing_collection.id} - {existing_collection.title}" # Keep consistent title for child link
                ))
            except Exception as e:
                logger.error(f"Failed to load existing collection {collection_path}: {e}. Will attempt to re-create if possible.")
                # If loading fails, proceed to create it
            continue
        logger.info(f"Processing dataset: {shortname} (ID: {opentopoid})")

        # Parse geometry and bbox from DB fields
        geometry = None
        if geojson:
            try:
                geometry = json.loads(geojson)
            except json.JSONDecodeError as e:
                logger.error(f"Invalid GeoJSON for {shortname}: {e}. Skipping geometry.")

        bbox = parse_extent(extent_box)
        if geometry and "coordinates" in geometry:
            coords = []
            if geometry["type"] == "Polygon":
                for ring in geometry["coordinates"]:
                    coords.extend(ring)
            elif geometry["type"] == "MultiPolygon":
                for poly in geometry["coordinates"]:
                    for ring in poly:
                        coords.extend(ring)
            if coords:
                coords = np.array(coords)
                xs, ys = coords[:, 0], coords[:, 1]
                calculated_bbox = [float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max())]
                if all(-181 <= c <= 181 for c in calculated_bbox):
                    bbox = calculated_bbox
                else:
                    logger.warning(f"Calculated bbox from GeoJSON for {shortname} is out of bounds: {calculated_bbox}. Using DB extent if available, else None.")
                    if not bbox:
                        bbox = None
            else:
                logger.warning(f"GeoJSON for {shortname} has no coordinates. Using DB extent if available, else None.")

        # Parse time interval
        interval = []
        start_dt_obj = ensure_datetime(start_date)
        end_dt_obj = ensure_datetime(end_date)
        if start_dt_obj and end_dt_obj:
            interval = [start_dt_obj, end_dt_obj]

        # Providers: OpenTopography + additional from DB
        providers = [{
            "name": "OpenTopography",
            "url": "https://www.opentopography.org",
            "roles": ["host"]
        }]
        providers.extend(get_additional_providers(otcollectionid))

        # Construct the collection title as <short_name> - <dataset_name>
        collection_title = f"{shortname} - {datasetname}"

        # Collection extra properties
        collection_properties = {
            "title": collection_title, # Assign the combined title
            "sci:citation": raw_citation,
            "providers": providers
        }
        if sci_doi and sci_doi.startswith("https://doi.org/"):
            collection_properties["sci:publications"] = [
                {"doi": sci_doi.replace("https://doi.org/", ""), "citation": raw_citation}
            ]

        collection_properties = {k: v for k, v in collection_properties.items() if v is not None}

        # Set up spatial/temporal extent
        spatial_extent = SpatialExtent([bbox]) if bbox else SpatialExtent([[None, None, None, None]])
        temporal_extent = TemporalExtent([interval]) if interval else TemporalExtent([[None, None]])
        extent = Extent(
            spatial=spatial_extent,
            temporal=temporal_extent
        )

        # Use the description from the database directly for the collection's description
        final_description = dataset_description if dataset_description else f"Detailed description for {collection_title} is not available."


        # Create STAC Collection object
        collection = Collection(
            id=opentopoid,
            description=final_description, # Use the database description
            license=use_license,
            extent=extent,
            extra_fields=collection_properties
        )

        # Declare extensions directly using stac_extensions property
        collection.stac_extensions = [
            "https://stac-extensions.github.io/scientific/v1.0.0/schema.json",
            "https://stac-extensions.github.io/file/v2.1.0/schema.json"
        ]

        # Add "about" link for the collection landing page
        collection.add_link(Link(
            rel="about",
            target=url,
            media_type="text/html",
            title="OpenTopography Dataset Landing Page"
        ))

        # NEW: Add a "citation" link for the formatted citation guidance
        # This link's title will contain the long citation text, visible on collection page.
        collection.add_link(Link(
            rel="cite-as", # Standard rel for citation information
            target="https://opentopography.org/citations", # Link to your general citation guidance page
            media_type="text/html", # The target is HTML documentation
            title=f"Citation Guidance: {formatted_citation_text}" # The guidance itself
        ))

        # Add license link if known
        if use_license and use_license.strip() in LICENSE_URLS:
            collection.add_link(Link(
                rel="license",
                target=LICENSE_URLS[use_license.strip()],
                media_type="text/html"
            ))

        collection_items = []
        layers = list(find_layers(dataset_path, shortname))

        if not layers:
            logger.info(f"No layers/items found under '{dataset_path}' for collection '{shortname}'. Creating an empty collection JSON.")
            # Even if no items, save the collection.
            collection.set_self_href(collection_path)
            collection.save_object(dest_href=collection_path)
            collection_objs.append(collection)
            # Create a simple child link for the root catalog that doesn't embed the description
            root_child_links.append(pystac.Link.child(
                target=collection, # Target is the collection object
                title=collection.title # Use the collection's title for the child link
            ))
            continue # Move to the next dataset


        for layer_name, layer_path in layers:
            vrt_path = find_vrt(dataset_path, layer_name)
            if not vrt_path:
                logger.warning(f"Skipping layer {layer_name}: No VRT file '{layer_name}.vrt' found in '{dataset_path}'.")
                continue

            logger.info(f"  Processing layer: {layer_name}")
            rasters = find_rasters_from_index(layer_path, index)
            if not rasters:
                logger.warning(f"    No rasters indexed for layer {layer_name} under {layer_path}. Skipping item creation for this layer.")
                continue

            assets = {}
            layer_bbox = None
            layer_datetimes = []

            for raster_file in rasters:
                asset_id = os.path.relpath(raster_file, layer_path).replace(os.sep, "_")
                asset_id = "".join(c for c in asset_id if c.isalnum() or c in ['_', '-']).strip('_').strip('-')
                if not asset_id:
                    asset_id = "raster_asset"

                bbox_from_index, dt_from_index = get_bbox_and_datetime_from_index(raster_file, index)
                asset_bbox, asset_geometry = get_asset_bbox_and_geometry_from_index(raster_file, index)

                # Initialize a dictionary to hold ALL extra fields for the Asset
                asset_extra_fields = {}

                # Safely add file size directly to asset_extra_fields
                try:
                    asset_extra_fields["file:size"] = os.path.getsize(raster_file)
                except Exception as e:
                    logger.warning(f"  [WARN] Could not get file size for {raster_file}: {e}")

                # If asset_bbox is available and valid, add it as a custom property
                # within the asset's extra_fields.
                # Note: 'bbox' directly in asset is not standard, use a custom name like 'raster_file_bbox'
                # or consider adding the 'projection' extension for 'proj:bbox' if needed.
                if asset_bbox:
                    asset_extra_fields["raster_file_bbox"] = asset_bbox # Custom property for asset's own bbox

                if bbox_from_index and all(isinstance(b, (int, float)) and -181 <= b <= 181 for b in bbox_from_index):
                    layer_bbox = update_bbox(layer_bbox, bbox_from_index)
                else:
                    logger.warning(f"  [WARN] Invalid bbox from index for {raster_file}: {bbox_from_index}. Skipping for layer aggregation.")

                if dt_from_index:
                    layer_datetimes.append(dt_from_index)

                # Create the Asset object, passing all extra fields in one dictionary
                current_asset = Asset(
                    href=raster_file,
                    media_type=pystac.MediaType.COG if raster_file.lower().endswith(('.tif', '.tiff')) else "application/octet-stream",
                    roles=["data"],
                    title=os.path.basename(raster_file), # Title for the individual asset
                    extra_fields=asset_extra_fields # Pass the collected extra fields here
                )
                assets[asset_id] = current_asset

            vrt_asset_id = os.path.basename(vrt_path).replace('.', '_')
            assets[vrt_asset_id] = Asset(
                href=vrt_path,
                media_type="application/xml",
                roles=["metadata"],
                title=os.path.basename(vrt_path) # Title for the VRT asset
            )

            display_name = get_display_layer_name(layer_name, shortname)
            item_geometry = minify_geometry(geometry) if geometry else None
            item_bbox = bbox if bbox else layer_bbox # Use collection bbox if layer_bbox not aggregateable

            # Build Item properties and create STAC Item
            props = {
                "title": f"{datasetname} - {display_name}", # REVERSED/COMBINED ITEM TITLE
                "description": f"Raster data for {display_name} from the {datasetname} collection."
            }

            item_datetime = None
            if layer_datetimes:
                item_datetime = min(layer_datetimes)
                props["start_datetime"] = min(layer_datetimes).isoformat() + "Z"
                props["end_datetime"] = max(layer_datetimes).isoformat() + "Z"
            elif interval:
                props["start_datetime"] = interval[0].isoformat() + "Z" if interval[0] else None
                props["end_datetime"] = interval[1].isoformat() + "Z" if interval[1] else None
                item_datetime = interval[0]
            else:
                item_datetime = datetime(1970, 1, 1)
                logger.warning(f"Item '{layer_name}' has no acquisition dates. Using sentinel datetime: {item_datetime}.")

            item = Item(
                id=layer_name,
                geometry=item_geometry,
                bbox=item_bbox,
                datetime=item_datetime,
                properties=props,
                stac_extensions=["https://stac-extensions.github.io/file/v2.1.0/schema.json"]
            )

            for k, v in assets.items():
                item.add_asset(k, v)

            item.collection_id = opentopoid
            item.set_self_href(os.path.join(ITEMS_DIR, f"{layer_name}.json"))
            item.save_object(dest_href=os.path.join(ITEMS_DIR, f"{layer_name}.json"))
            collection_items.append(item)
            logger.debug(f"    Saved item: {layer_name}.json")


        for item in collection_items:
            collection.add_item(item)

        collection.set_self_href(collection_path)
        collection.save_object(dest_href=collection_path)
        collection_objs.append(collection) # Add the full collection object to list
        logger.info(f"  Saved collection: {collection_path}")

        # MANUALLY CREATE A LEAN CHILD LINK FOR THE ROOT CATALOG
        # This link will not embed the full collection description,
        # relying on clients to fetch the collection JSON for details.
        # Construct a pystac.Link object directly for full control.
        root_child_links.append(pystac.Link(
            rel=pystac.RelType.CHILD, # Explicitly set the relation type
            target=os.path.basename(collection_path), # 'target' is the parameter for the href/URL in the Link constructor
            title=collection.title,
            media_type=pystac.MediaType.JSON
        ))

    # Build the root catalog with all child collections
    root_catalog_path = os.path.join(STAC_ROOT, "ot_raster_collection.json")
    root_catalog = Catalog(
        id="ot_raster_collection",
        description="Top-level STAC Catalog for all raster dataset collections"
    )

    # Add the manually created lean child links to the root catalog
    for link in root_child_links:
        root_catalog.add_link(link)

    root_catalog.set_self_href(root_catalog_path)
    root_catalog.save(catalog_type=pystac.CatalogType.SELF_CONTAINED)
    logger.info(f"Root catalog saved at {root_catalog_path}")

    # Fix links and build STAC index
    fix_links_in_json_files(STAC_ROOT, BASE_URL, ITEMS_URL, DATA_URL_PREFIX)
    build_stac_index(STAC_ROOT, ITEMS_DIR, STAC_INDEX_CSV)

    # --- Timing: End ---
    rest_end = time.time()
    total_time = rest_end - script_start
    rest_time = rest_end - rest_start
    logger.info(f"Rest of script runtime: {rest_time:.2f} seconds")
    logger.info(f"Total script runtime: {total_time:.2f} seconds")
    logger.info("STAC Catalog Builder finished.")

if __name__ == "__main__":
    main()

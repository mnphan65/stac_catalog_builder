# STAC Catalog Builder for Raster Datasets

This script builds a [STAC (SpatioTemporal Asset Catalog)](https://stacspec.org/) catalog for a collection of raster datasets. It integrates metadata from a PostgreSQL database, scans the file system for raster files, and outputs a complete STAC-compliant JSON catalog structure with collections, items, and assets.

---

## Features

- **Database Integration:** Extracts collection and provider metadata from a PostgreSQL database.
- **Flexible Raster Scanning:** Indexes raster files (including `.tif`, `.tiff`, `.img`, `.adf`, `.flt`, `.grd`) from a directory tree, capturing file paths, modification times, and bounding boxes.
- **STAC Catalog Generation:** Builds STAC collections and items, populates them with assets and metadata, and writes self-contained JSON files.
- **Link Fixup:** Rewrites catalog/collection/item links to resolve to public URLs.
- **Index CSVs:** Outputs CSV indices for raster files and generated STAC items for quick lookup.

---

## Prerequisites

- Python 3.7+
- A PostgreSQL database with the expected schema
- Python packages:
  - [pystac](https://pystac.readthedocs.io/)
  - [psycopg2](https://www.psycopg.org/)
  - [rasterio](https://rasterio.readthedocs.io/)
  - [numpy](https://numpy.org/)
  - [python-dateutil](https://dateutil.readthedocs.io/)

Install dependencies:
```bash
pip install pystac psycopg2 rasterio numpy python-dateutil
```

---

## Configuration

Set the following variables at the top of the script to match your environment:

| Variable           | Purpose                                      |
|--------------------|----------------------------------------------|
| `RASTER_ROOT`      | Root directory containing raster datasets    |
| `STAC_ROOT`        | Output directory for STAC catalog structure  |
| `BASE_URL`         | Base public URL for the catalog              |
| `ITEMS_URL`        | Public URL prefix for STAC items             |
| `DATA_URL_PREFIX`  | Public URL prefix for raster data            |
| `DB_CONN`          | PostgreSQL connection parameters             |

---

## Folder Structure (Example)

```
/datacloud/raster/
    ShortName1/
        ShortName1_be/
            file1.tif
            ...
        ShortName1_be.vrt
    ShortName2/
        ...
/home/opentopo/apps/tomcat9/webapps/STAC/
    ot_raster_collection.json      # Root catalog
    ShortName1_collection.json
    ShortName2_collection.json
    items/
        ShortName1_be.json
        ...
    raster_index.csv
    stac_index.csv
```

---

## Script Overview

1. **Connects to the database** to fetch dataset and provider metadata.
2. **Indexes raster files**, capturing file path, last modification time, and bounding box (in EPSG:4326).
3. **Creates STAC Collections** for each dataset, with metadata, providers, and spatial/temporal extents.
4. **Creates STAC Items** for each data layer (based on VRTs), associating raster files as assets.
5. **Writes all STAC objects** (catalog, collections, items) as JSON in the output directory.
6. **Rewrites all links** in the JSON files to use public URLs.
7. **Outputs CSV indices** for rasters and items for quick lookup.

---

## Key Functions

### db_connect()
Connects to the PostgreSQL database using `DB_CONN` parameters.

### get_collections_from_db()
Queries database for raster datasets and their metadata.

### get_additional_providers(collection_id)
Gets provider information for a specific collection from the database.

### parse_extent(extent_box), bbox_to_polygon(bbox), parse_bbox(val)
Helpers for parsing and converting spatial extents and bounding boxes.

### build_raster_index(raster_root, index_csv)
Recursively scans the raster data directory, writing a CSV with file path, mtime, and bbox.

### build_stac_index(stac_root, items_dir, stac_index_csv)
Indexes all STAC items by id, title, and file path.

### find_layers(dataset_path, dataset_id)
Finds data layers (typically directories named with the dataset id prefix).

### find_vrt(dataset_path, layer_name)
Locates the VRT file for a given layer.

### find_rasters_from_index(layer_path, index)
Finds raster files (from the index) within a given layer path.

### get_bbox_and_datetime_from_index(raster_path, index)
Returns bbox and modification time for a raster file from the index.

### update_bbox(current_bbox, new_bbox)
Expands a bounding box to include another bbox.

### get_asset_bbox_and_geometry_from_index(raster_path, index)
Gets bbox and geometry for a raster asset from the index or reads it directly if missing.

### fix_links_in_json_files(stac_root, base_url, items_url, data_url_prefix)
Rewrites all links in catalog/collection/item JSONs to use public URLs.

### ensure_datetime(dt)
Converts date/datetime strings to Python `datetime` objects.

### minify_geometry(geom_dict)
Minifies a geometry dictionary for compactness.

### get_display_layer_name(layer_name, shortname)
Maps internal layer names to display-friendly names (e.g., appends `_DTM` or `_DSM`).

### main()
Orchestrates the catalog build process:
- Creates directories and root catalog if needed
- Indexes raster files
- Builds STAC collections and items
- Fixes up links and writes indices

---

## Running the Script

Ensure your environment and dependencies are set up, then run:

```bash
python stac_catalog_builder.py
```

All output will be written to the configured `STAC_ROOT`.

---

## Customization & Notes

- Adapt SQL queries in `get_collections_from_db` and `get_additional_providers` for your database schema if needed.
- Adjust file structure logic if your raster archive is organized differently.
- Add or modify STAC fields as needed by editing the relevant parts of the script.
- The script will skip datasets whose collection JSON already exists.

---

## License

The script is provided without warranty. Modify and use as needed for your data and workflow.

---

## References

- [STAC Specification](https://stacspec.org/)
- [PySTAC Documentation](https://pystac.readthedocs.io/)
- [OpenTopography](https://opentopography.org/)

---

For further questions or improvements, open an issue or PR!

# Environment

Default ArcGIS Pro Python environment on the GIS server:
`C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe`

numpy, pandas, scipy, rasterio, geopandas, scikit-image, and pyarrow are normally present. Install once:
```
conda install -n arcgispro-py3 -c conda-forge laspy lazrs-python pyyaml python-dotenv
```
`lazrs-python` is needed for `.laz`. The `arcpy` engine in 00a needs 3D Analyst and Spatial Analyst.

Point `lidar.local_scratch` at local disk with at least 20 GB free; keep `lidar.workers` at 2 to 4.

R (second pass): `install.packages(c("lidR", "sf", "future", "terra"))`.

`src/c2t` (00c) uses numpy, scipy, pandas, geopandas, shapely, rasterio, scikit-image, scikit-learn, and joblib, all in `arcgispro-py3`, plus `laspy` and `lazrs-python` above. Optional: `pip install cloth-simulation-filter` for `c2t.ground: csf`; `pytest` and `nbconvert` for `python -m pytest tests -q` and `scripts/run_c2t.py`. External data (about 7 GB) goes to `data/raw/ext` via `c2t.get_data()`.

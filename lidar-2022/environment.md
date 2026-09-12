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

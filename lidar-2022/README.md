# lidar-2022

2022 Basin LiDAR base processing: LAZ archive to DTM, 1 m CHM, height-normalized LAZ, 30 m canopy metrics, and TAOs, published to `\\vcenter2\GIS_DATA\LiDAR\2022\Derived`. Downstream users: `trpa-agency/ForestHealth/plot-network-design` (forest health plot network) and LITIDA. Read `docs/LIDAR_HANDOFF.md`, then `docs/SERVER_RUN.md`. Run `00a_las_to_chm` (pre-flight first), then `scripts/run_lidar.py --with-chm-metrics --with-taos` headless on the server.

`00c_cloud2trees` is the Python port of the cloud2trees R package (`src/c2t`, `docs/C2T_PORT.md`): per-tree DBH from FIA allometry, forest type, competition, crown base height, crown biomass, and QUIC-Fire inputs, run on tile subsets and on the 00b tree table. `python -m pytest tests -q` checks it on synthetic data.

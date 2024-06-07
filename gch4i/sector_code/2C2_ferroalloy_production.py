# %%
# Name: 2C2_ferroalloy_production.py

# Authors Name: N. Kruskamp, H. Lohman (RTI International), Erin McDuffie (EPA/OAP)
# Date Last Modified: 5/10/2024
# Purpose: Spatially allocates methane emissions for source category 2C2 Ferroalloy production
#
# Input Files:
#      - State_Ferroalloys_1990-2021.xlsx, SubpartK_Ferroalloy_Facilities.csv,
#           all_ghgi_mappings.csv, all_proxy_mappings.csv
# Output Files:
#      - f"{SECTOR_NAME}_ch4_kt_per_year.tif, f"{SECTOR_NAME}_ch4_emi_flux.tif"
# Notes:
# TODO: update to use facility locations from 2024 GHGI state inventory files
# TODO: include plotting functionaility
# TODO: include netCDF writting functionality

# ---------------------------------------------------------------------
# %% STEP 0. Load packages, configuration files, and local parameters

import calendar
import warnings
from pathlib import Path

import osgeo  # noqa
import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import rasterio.enums
import seaborn as sns
from IPython.display import display
import duckdb
from geopy.geocoders import Nominatim

# from pytask import Product, task
from rasterio.features import rasterize

from gch4i.config import (
    ghgi_data_dir_path,
    global_data_dir_path,
    max_year,
    min_year,
    tmp_data_dir_path,
    data_dir_path,
)
from gch4i.gridding import ARR_SHAPE, GEPA_PROFILE
from gch4i.utils import (
    calc_conversion_factor,
    load_area_matrix,
    write_tif_output,
    write_ncdf_output,
    tg_to_kt,
    name_formatter,
)

# %% # Set paths to input EPA inventory data, proxy mapping files, and proxy data
# Set output paths

# @mark.persist
# @task
# def task_sector_industry_ferroalloy():
#     pass

# https://www.epa.gov/system/files/documents/2024-02/us-ghg-inventory-2024-main-text.pdf
IPCC_ID = "2C2"
SECTOR_NAME = "industry"
SOURCE_NAME = "ferroalloy production"
FULL_NAME = "_".join([IPCC_ID, SECTOR_NAME, SOURCE_NAME]).replace(" ", "_")

netcdf_title = f"EPA methane emissions from {SOURCE_NAME}"
netcdf_description = (
    f"Gridded EPA Inventory - {SECTOR_NAME} - "
    f"{SOURCE_NAME} - IPCC Source Category {IPCC_ID}"
)
# OUTPUT FILES
ch4_kt_dst_path = tmp_data_dir_path / f"{FULL_NAME}_ch4_kt_per_year"
ch4_flux_dst_path = tmp_data_dir_path / f"{FULL_NAME}_ch4_emi_flux"

# INPUT FILES
# NOTE: we get the emissions and the proxy from the same file for this source.
EPA_inputfile = Path(ghgi_data_dir_path / "State_Ferroalloys_1990-2021.xlsx")

state_geo_path = global_data_dir_path / "tl_2020_us_state.zip"

# subpart data are retrieve via the API
subart_k_api_url = (
    "https://data.epa.gov/efservice/k_subpart_level_information/"
    "pub_dim_facility/ghg_name/=/Methane/CSV"
)
# FRS data are save as a file in our data directory
frs_path = Path(
    (
        "C:/Users/nkruskamp/Environmental Protection Agency (EPA)/"
        "Gridded CH4 Inventory - Task 2/ghgi_v3_working/v3_data/global/"
        "NATIONAL_FACILITY_FILE.CSV"
    )
)

# EEM NOTE: in some scripts we use 0.01x0.01 degree resolution area matrix,
# in other scripts with use the 0.1x0.1 area matrix. Can we add both to this function
# and specify which we're using here?
area_matrix = load_area_matrix()
# ----------------------------------------------------------------------------

# %% STEP 1. Load GHGI-Proxy Mapping Files

# NOTE: looking at rework of the proxy mapping files into an aggregate flat file
# that would then be formed into a proxy dictionary to retain the existing approach
# but allow for interrogation of the objects when needed.
# EEM: updated to v3 path
# proxy_mapping_dir = Path(
#    "C:/Users/nkruskamp/Research Triangle Institute/EPA Gridded Methane - Task 2/data"
# )
# ghgi_map_path = proxy_mapping_dir / "all_ghgi_mappings.csv"
# proxy_map_path = proxy_mapping_dir / "all_proxy_mappings.csv"
ghgi_map_path = data_dir_path / "all_ghgi_mappings.csv"
proxy_map_path = data_dir_path / "all_proxy_mappings.csv"
ghgi_map_df = pd.read_csv(ghgi_map_path)
proxy_map_df = pd.read_csv(proxy_map_path)

proxy_map_df.query("GHGI_Emi_Group == 'Emi_Ferro'").merge(
    ghgi_map_df.query("GHGI_Emi_Group == 'Emi_Ferro'"), on="GHGI_Emi_Group"
)

ferro_map_data_dict = {}
ferro_map_data_dict["Emi_Ferro"] = {
    "Map_Ferro": {
        "facility_data_file": EPA_inputfile,
        "sheet_name": "Ferroalloy Calculations",
    }
}

# -----------------------------------------------------------------------
# %% STEP 2: Read In EPA State GHGI Emissions by Year

# read in the ch4_kt values for each state
EPA_ferro_emissions = (
    # read in the data
    pd.read_excel(
        EPA_inputfile,
        sheet_name="InvDB",
        skiprows=15,
        nrows=115,
        usecols="A:AO",
    )
    # name column names lower
    .rename(columns=lambda x: str(x).lower())
    # drop columns we don't need
    .drop(
        columns=[
            "sector",
            "source",
            "subsource",
            "fuel",
            "subref",
            "2nd ref",
            "exclude",
        ]
    )
    # get just methane emissions
    .query("ghg == 'CH4'")
    # remove that column
    .drop(columns="ghg")
    # set the index to state
    .rename(columns={"state": "state_code"})
    .set_index("state_code")
    # covert "NO" string to numeric (will become np.nan)
    .apply(pd.to_numeric, errors="coerce")
    # drop states that have all nan values
    .dropna(how="all")
    # reset the index state back to a column
    .reset_index()
    # make the table long by state/year
    .melt(id_vars="state_code", var_name="year", value_name="ch4_tg")
    .assign(ch4_kt=lambda df: df["ch4_tg"] * tg_to_kt)
    .drop(columns=["ch4_tg"])
    # make the columns types correcet
    .astype({"year": int, "ch4_kt": float})
    .fillna({"ch4_kt": 0})
    # get only the years we need
    .query("year.between(@min_year, @max_year)")
)
EPA_ferro_emissions.head()

# %%
# QA/QC - check counts of years of state data and plot by state_code
display(EPA_ferro_emissions["state_code"].value_counts())
display(EPA_ferro_emissions["year"].min(), EPA_ferro_emissions["year"].max())

# a quick plot to verify the values
sns.relplot(
    kind="line",
    data=EPA_ferro_emissions,
    x="year",
    y="ch4_kt",
    hue="state_code",
    # legend=False,
)
# ------------------------------------------------------------------------


# %% STEP 3: GET AND FORMAT PROXY DATA
# TODO: explore if it makes sense to write a function / task for every proxy.
# there are about 65 unique ones.
# EEM: this sounds like a good apprach. I'll leave that decision to RTI
def task_map_ferro_proxy():
    pass


# Get state codes and names to join with facility information to make merging with
# the facility location data match.

state_gdf = (
    gpd.read_file(state_geo_path)
    .loc[:, ["NAME", "STATEFP", "STUSPS", "geometry"]]
    .rename(columns=str.lower)
    .rename(columns={"stusps": "state_code", "name": "state_name"})
    .astype({"statefp": int})
    # get only lower 48 + DC
    .query("(statefp < 60) & (statefp != 2) & (statefp != 15)")
    .to_crs(4326)
)
state_gdf
# %%

# The facilities have multiple reporting units for each year. This will read in the
# facilities data and compute the facility level sum of ch4_kt emissions for each
# year. This pulls from the raw table but ends in the same form as the table on sheet
# "GHGRP_kt_Totals"

# read in the SUMMARY facilities emissions data
ferro_facilities_df = (
    pd.read_excel(
        EPA_inputfile,
        sheet_name="Ferroalloy Calculations",
        skiprows=72,
        nrows=12,
        usecols="A:AI",
    )
    .rename(columns=lambda x: str(x).lower())
    .rename(columns={"facility": "facility_name", "state": "state_name"})
    .drop(columns=["year opened"])
    .melt(id_vars=["facility_name", "state_name"], var_name="year", value_name="ch4_kt")
    .astype({"year": int})
    .assign(formatted_fac_name=lambda df: name_formatter(df["facility_name"]))
    .merge(state_gdf[["state_code", "state_name"]], on="state_name")
    .query("year.between(@min_year, @max_year)")
)
# create a table of unique facility names to match against the facility locations data.
unique_facilities = (
    ferro_facilities_df[["formatted_fac_name", "state_code"]]
    .drop_duplicates()
    .reset_index(drop=True)
)
unique_facilities
ferro_facilities_df
# %%
# quick timeseries look at the data.
g = sns.lineplot(
    ferro_facilities_df,
    x="year",
    y="ch4_kt",
    hue="facility_name",
    legend=True,
)
sns.move_legend(g, "upper left", bbox_to_anchor=(1.0, 0.9))
sns.despine()
# %%
# STEP 2.1: Get and format FRS and Subpart K facility locations data
# this will read in both datasets, format the columns for the facility name, 2 letter
# state code, latitude and longitude. Then merge the two tables together, and create
# a formatted facility name that eases matching of our needed facilities to the location
# data

frs_raw = duckdb.execute(
    ("SELECT primary_name, state_code, latitude83, longitude83 " f"FROM '{frs_path}' ")
).df()

subpart_k_raw = pd.read_csv(subart_k_api_url)
# %%
frs_df = (
    frs_raw.rename(columns=str.lower)
    .drop_duplicates(subset=["primary_name", "state_code"])
    .rename(columns={"primary_name": "facility_name"})
    .rename(columns=lambda x: x.strip("83"))
    .dropna(subset=["latitude", "longitude"])
)

subpart_k_df = (
    subpart_k_raw.drop_duplicates(subset=["facility_name", "state"])
    .rename(columns={"state": "state_code"})
    .loc[
        :,
        ["facility_name", "state_code", "latitude", "longitude"],
    ]
    .dropna(subset=["latitude", "longitude"])
)

# merge the two datasets together, format the facility name, drop any duplicates.
facility_locations = (
    pd.concat([subpart_k_df, frs_df], ignore_index=True)
    .assign(formatted_fac_name=lambda df: name_formatter(df["facility_name"]))
    .drop_duplicates(subset=["formatted_fac_name", "state_code"])
)
facility_locations

# %%
# try to match facilities based on the formatted name and state code
matching_facilities = unique_facilities.merge(
    facility_locations, on=["formatted_fac_name", "state_code"], how="left"
)

display(matching_facilities)
# %%
# NOTE: We are missing 4 facilities. Manually, we checked the 4 missing facilities
# for partial name matches in the subpart/frs facility data. We found 3 facilities.
# 1 was still missing. Using partial matching of name and state, we assign the coords
# to our records.

matching_facilities.loc[
    matching_facilities["formatted_fac_name"] == "bear metallurgical co",
    ["facility_name", "latitude", "longitude"],
] = (
    facility_locations[
        facility_locations["formatted_fac_name"].str.contains("bear metal")
        & facility_locations["state_code"].eq("PA")
    ]
    .loc[:, ["facility_name", "latitude", "longitude"]]
    .values
)

matching_facilities.loc[
    matching_facilities["formatted_fac_name"] == "stratcor inc",
    ["facility_name", "latitude", "longitude"],
] = (
    facility_locations[
        facility_locations["formatted_fac_name"].str.contains("stratcor")
        & facility_locations["state_code"].eq("AR")
    ]
    .loc[:, ["facility_name", "latitude", "longitude"]]
    .values
)

matching_facilities.loc[
    matching_facilities["formatted_fac_name"] == "metallurg vanadium corp",
    ["facility_name", "latitude", "longitude"],
] = (
    facility_locations[
        facility_locations["formatted_fac_name"].str.contains("vanadium inc")
        & facility_locations["state_code"].eq("OH")
    ]
    .loc[:, ["facility_name", "latitude", "longitude"]]
    .values
)

display(matching_facilities)
# %%
# NOTE: The final facility, thompson creek, has no name match in the facilities data,
# so we use the city and state provided in the inventory data to get its location.
# https://en.wikipedia.org/wiki/Thompson_Creek_Metals.
# thompson creek metals co inc was acquired in 2016 by Centerra Gold.
# A search for Centerra in PA turns up 2 records in the combine subpart K and FRS
# dataset. But Centerra Co-op is an agricultural firm, not a ferroalloy facility.
# https://www.centerracoop.com/locations
# display(
#     facility_locations[
#         facility_locations["formatted_fac_name"].str.contains("centerra")
#         & facility_locations["state_code"].eq("PA")
#     ]
# )

fac_locations = pd.read_excel(
    EPA_inputfile,
    sheet_name="USGS_2008_Facilities",
    skiprows=4,
    nrows=12,
    usecols="A:C",
).drop(columns="Unnamed: 1")
t_creek_city_state = fac_locations.loc[
    fac_locations["Company"] == "Thompson Creek Metals Co.", "Plant location"
].values[0]

geolocator = Nominatim(user_agent="RTI testing")
t_creek_loc = geolocator.geocode(t_creek_city_state)

matching_facilities.loc[
    matching_facilities["formatted_fac_name"] == "thompson creek metals co inc",
    ["latitude", "longitude"],
] = [t_creek_loc.latitude, t_creek_loc.longitude]

matching_facilities

# %% QA/QC
ferro_facilities_gdf = ferro_facilities_df.merge(
    matching_facilities.drop(columns="facility_name"),
    on=["formatted_fac_name", "state_code"],
    how="left",
)
ferro_facilities_gdf = gpd.GeoDataFrame(
    ferro_facilities_gdf.drop(columns=["latitude", "longitude"]),
    geometry=gpd.points_from_xy(
        ferro_facilities_gdf["longitude"], ferro_facilities_gdf["latitude"], crs=4326
    ),
)

# make sure the merge gave us the number of results we expected.
if not (ferro_facilities_gdf.shape[0] == ferro_facilities_df.shape[0]):
    print("WARNING the merge shape does not match the original data")
ferro_facilities_gdf
# %% # save a shapefile of the v3 ferro facilities for reference
fac_locations = ferro_facilities_gdf.dissolve("formatted_fac_name")
fac_locations[fac_locations.is_valid].loc[:, ["geometry"]].to_file(
    tmp_data_dir_path / "v3_ferro_facilities.shp.zip", driver="ESRI Shapefile"
)

# %% QA/QC

# some checks of the data
# how many na values are there
print("Number of NaN values:")
display(ferro_facilities_gdf.isna().sum())
# how many missing locations are there by year
print("Number of Facilities with Missing Locations Each Year")
display(ferro_facilities_gdf[ferro_facilities_gdf.is_empty]["year"].value_counts())
# how many missing locations are there by facility name
print("For Each Facility with Missing Data, How Many Missing Years")
display(
    ferro_facilities_gdf[ferro_facilities_gdf.is_empty][
        "formatted_fac_name"
    ].value_counts()
)
# a plot of the timeseries of emission by facility
sns.lineplot(
    data=ferro_facilities_gdf, x="year", y="ch4_kt", hue="facility_name", legend=False
)

# ----------------------------------------------------------------------------
# %% STEP 4: ALLOCATION OF STATE / YEAR EMISSIONS TO PROXIES
#         (BY PROXY FRACTION IN EACH GRIDCELL)
# For this source, state-level emissions are spatially allocated using the
#   the fraction of facility-level emissions within each grid cell in each state,
#   for each year
# EEM: question - for sources where we go from the state down to the grid-level,
#      will we still have this calculation step, or will we go straight to the
#      rasterize step?

# This does the allocation for us in a function by state and year.


def state_year_allocation_emissions(fac_emissions, inventory_df):

    # fac_emissions are the total emissions for the facilities located in that state
    # and year. It can be one or more facilities. Inventory_df EPA state GHGI summary
    # emissions table

    # get the target state and year
    state, year = fac_emissions.name
    # get the total proxy data (e.g., emissions) within that state and year.
    # It will be a single value.
    emi_sum = inventory_df[
        (inventory_df["state_code"] == state) & (inventory_df["year"] == year)
    ]["ch4_kt"].iat[0]

    # allocate the EPA GHGI state emissions to each individual facility based on their
    # proportion emissions (i.e., the fraction of total state-level emissions occuring at each facility)
    allocated_fac_emissions = ((fac_emissions / fac_emissions.sum()) * emi_sum).fillna(
        0
    )
    return allocated_fac_emissions


# we create a new column that assigns the allocated summary emissions to each facility
# based on its proportion of emission to the facility totals for that state and year.
# so for each state and year in the summary emissions we apply the function.
ferro_facilities_gdf["allocated_ch4_kt"] = ferro_facilities_gdf.groupby(
    ["state_code", "year"]
)["ch4_kt"].transform(state_year_allocation_emissions, inventory_df=EPA_ferro_emissions)
# %% QA/QC
# We now check that the sum of facility emissions equals the EPA GHGI emissions by state
# and year. The resulting sum_check table shows you where the emissions data DO NOT
# equal and need more investigation.
sum_check = (
    ferro_facilities_gdf.groupby(["state_code", "year"])["allocated_ch4_kt"]
    .sum()
    .reset_index()
    .merge(EPA_ferro_emissions, on=["state_code", "year"], how="outer")
    .assign(
        check_diff=lambda df: df.apply(
            lambda x: np.isclose(x["allocated_ch4_kt"], x["ch4_kt"]), axis=1
        )
    )
)
display(sum_check[~sum_check["check_diff"]])

print(
    (
        "states with no facilities in them: "
        f"{EPA_ferro_emissions[~EPA_ferro_emissions['state_code'].isin(ferro_facilities_gdf['state_code'])]['state_code'].unique()}"
    )
)
print(
    (
        "states with unaccounted emissions: "
        f"{sum_check[~sum_check['check_diff']]['state_code'].unique()}"
    )
)

# %%

# STEP 5: RASTERIZE THE CH4 KT AND FLUX
# e.g., calculate fluxes and place the facility-level emissions on the CONUS grid

# for each year, grid the adjusted emissions data in kt and do conversion for flux.
ch4_kt_result_rasters = {}
ch4_flux_result_rasters = {}

for year, data in ferro_facilities_gdf.groupby("year"):
    month_days = [calendar.monthrange(year, x)[1] for x in range(1, 13)]
    year_days = np.sum(month_days)

    ch4_kt_raster = rasterize(
        shapes=[
            (shape, value)
            for shape, value in data[["geometry", "allocated_ch4_kt"]].values
        ],
        out_shape=ARR_SHAPE,
        fill=0,
        transform=GEPA_PROFILE["transform"],
        dtype=np.float64,
        merge_alg=rasterio.enums.MergeAlg.add,
    )

    conversion_factor_annual = calc_conversion_factor(year_days, area_matrix)
    ch4_flux_raster = ch4_kt_raster * conversion_factor_annual

    ch4_kt_result_rasters[year] = ch4_kt_raster
    ch4_flux_result_rasters[year] = ch4_flux_raster
# --------------------------------------------------------------------------


# %%
# STEP 6: SAVE THE FILES

# check the sums all together now...
# TODO: report QC metrics for both kt and flux values

for year, raster in ch4_kt_result_rasters.items():
    raster_sum = raster.sum()
    fac_sum = ferro_facilities_gdf.query("year == @year")["allocated_ch4_kt"].sum()
    emi_sum = EPA_ferro_emissions.query("year == @year")["ch4_kt"].sum()
    missing_sum = sum_check[(~sum_check["check_diff"]) & (sum_check["year"] == year)][
        "ch4_kt"
    ].sum()

    print(year)
    print(
        "does the raster sum equal the facility sum: "
        f"{np.isclose(raster_sum, fac_sum)}"
    )
    print(
        "does the raster sum equal the national total: "
        f"{np.isclose(raster_sum, emi_sum)}"
    )
    # this shows we are consistent with our missing emissions where the states with no
    # facilities make up the difference, as we would expect.
    print(
        "do the states with no facilities equal the missing amount: "
        f"{np.isclose((emi_sum - raster_sum), missing_sum)}"
    )
    print()
# %%
print("checking gridded result values by year.")
check_sum_dict = {}
for year, arr in ch4_kt_result_rasters.items():
    gridded_sum = arr.sum()
    check_sum_dict[year] = gridded_sum

gridded_year_sums_df = (
    pd.DataFrame()
    .from_dict(check_sum_dict, orient="index")
    .rename(columns={0: "gridded_sum"})
)

emissions_by_year_check = (
    EPA_ferro_emissions.groupby("year")["ch4_kt"]
    .sum()
    .to_frame()
    .join(gridded_year_sums_df)
    .assign(isclose=lambda df: np.isclose(df["ch4_kt"], df["gridded_sum"]))
)
all_equal = emissions_by_year_check["isclose"].all()

print(f"do all gridded emission by year equal (isclose): {all_equal}")
if not all_equal:
    print("if not, these ones below DO NOT equal")
    display(emissions_by_year_check[~emissions_by_year_check["isclose"]])
# %% Write files

write_tif_output(ch4_kt_result_rasters, ch4_kt_dst_path)
write_tif_output(ch4_flux_result_rasters, ch4_flux_dst_path)
write_ncdf_output(
    ch4_flux_result_rasters,
    ch4_flux_dst_path.with_suffix(".nc"),
    netcdf_title,
    netcdf_description,
)
# ------------------------------------------------------------------------

# %% STEP 7: PLOT THE DATA FOR REFERENCE
# TODO: write visual outputs for QC check
# %%

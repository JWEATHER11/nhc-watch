#!/usr/bin/env python3
"""
metar_storm_pipeline.py -- Real-observation "a storm is actually
happening right now" watcher, using live METAR/ASOS station reports
across the SETX/SWLA corridor -- NOT model output (per instruction:
"i dont want to use model data to tell me if real storms are
happening, model data can be wrong"). This is ground truth: a station
reporting "TS" is a human/automated instrument confirming a
thunderstorm is physically overhead right now.

Deliberately does NOT try to be radar -- true gap-free radar coverage
(NEXRAD/MRMS) is a separate, bigger project (raw binary decode, real
radar signal processing). This is the lighter-weight, still-genuinely-
real complement: point observations at corridor ASOS/AWOS airports
(temp/wind/present-weather), plus a much denser layer of NOAA/USGS
HADS/DCP drainage-district rain gauges (precip only, per instruction
for maximum station density around Beaumont/Port Arthur/Orange/
Winnie/Jasper/Houston) -- both free, both no signup required.

Alerts only on a hazard NEWLY appearing at a station (not a repeat of
an already-alerted, still-ongoing condition), per the same "don't
send the same stuff over and over" principle applied to the AFD
throttle earlier. Sent to the NWS Telegram chat, alongside Houston
and Lake Charles content -- this is real-observation/radar-adjacent
work, kept separate from the model-based WXMODEL chat.
"""

import csv
import io
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

STATE_FILE = Path(__file__).parent / "metar_storm_state.json"
BEAUMONT_TZ = ZoneInfo("America/Chicago")
MAX_ATTEMPTS = 2
RETRY_DELAY_SEC = 2

# Expanded from the original 7 to real, verified IEM ASOS/AWOS
# stations across the SETX/SWLA corridor -- per instruction to add
# more station coverage for catching rain starting, heavy totals, and
# wind gusts. JAS (Jasper) closes a real gap: HRRR flagged a wind gust
# near Jasper earlier with no station there to confirm it on the
# ground. Verified each of these actually reports live via IEM before
# adding (mesonet.agron.iastate.edu/geojson/network/TX_ASOS.geojson
# and LA_ASOS.geojson), not guessed from memory.
CORRIDOR_STATIONS = {
    "BPT": "Beaumont/Port Arthur",
    "BMT": "Beaumont Municipal",
    "IAH": "Houston Intercontinental",
    "HOU": "Houston Hobby",
    "DWH": "Houston/D.W. Hooks (north Houston)",
    "EFD": "Houston/Ellington (south Houston)",
    "SGR": "Sugar Land",
    "CXO": "Conroe/Montgomery County",
    "LVJ": "Pearland",
    "GLS": "Galveston",
    "LCH": "Lake Charles",
    "CWF": "Lake Charles/Chennault",
    "LFT": "Lafayette",
    "LFK": "Lufkin",
    "JAS": "Jasper",
    "ORG": "Orange",
    "DRI": "De Ridder",
    "3R7": "Jennings",
    "UTS": "Huntsville",
    "T78": "Liberty",
}

# Added per instruction: "as many weather stations as possible around
# Beaumont/Port Arthur/Orange/Winnie/Jasper/near Houston" -- these are
# NOAA/USGS-fed drainage-district and flood-monitoring rain gauges
# (HADS/DCP network), free via the same IEM feed as the ASOS stations
# above, no signup, no API key. Verified live against IEM's hads.py
# CGI endpoint before adding: 340 stations in this corridor genuinely
# report hourly precipitation (SHEF code PPHRRZZ, with PPERRZZ/
# PPHRGZZ/PPURRZZ/PCIRGZZ as fallbacks some stations use instead) --
# not guessed from memory. These gauges don't have temp/wind/present-
# weather sensors, so only the heavy-rain hazard applies to them.
DCP_CORRIDOR_STATIONS = {
    "ABWT2": "The Woodlands - Alden Branch",
    "AIRT2": "HALLS BAYOU AT AIRLINE DRIVE",
    "ASCT2": "Armand Bayou  AT CLear Lake Park",
    "BBBT2": "BRAYS BAYOU AT BELLAIRE BLVD.",
    "BBET2": "The Woodlands - Bear Branch",
    "BBMT2": "Buffalo Bayou  AT Milam Drive",
    "BBWT2": "BRAYS BAYOU @ BELTWAY 8",
    "BETT2": "BEAUMONT SCAN",
    "BFOT2": "Berry Bayou  AT Forest Oaks",
    "BGBT2": "Deer Park 1SW - Boggy Bayou",
    "BGYT2": "League City 4SSE - Bordens Gully",
    "BHGT2": "Spring Shadows 2ENE - Brickhouse Gully",
    "BIPT2": "Pine Island Bayou  AT B.I. Pump Plant",
    "BKHT2": "Brickhouse Gully  AT Costa Rica Street",
    "BKUT2": "Egypt 2SE - Bear Branch",
    "BMDT2": "Beamer Ditch In Houston",
    "BMLT2": "Greens Bayou  AT Bammel North Houston",
    "BMPT2": "Houston - MLK Boulevard",
    "BNKT2": "BUNKER HILL VILLAGE",
    "BNPT2": "BERRY BAYOU @ NEVADA",
    "BOBT2": "League City 2SSE - Benson Bayou",
    "BOOT2": "Pearland 1W - Brazoria Drainage",
    "BSBT2": "SPRING VALLEY",
    "BSFT2": "Buffalo Bayou  AT San Felipe Road",
    "BUUT2": "Fairbanks 3SSW - Buttermilk Creek",
    "BZNT2": "Sienna Plantation 2SW - Brazos River",
    "CBAT2": "Clear Creek  AT Bay Area Boulevard",
    "CBHT2": "Beach City 5W - Cedar Bayou",
    "CBLT2": "Mont Belvieu 3ESE - Cotton Bayou",
    "CBPT2": "CARPENTERS BAYOU AT I 10",
    "CCBT2": "Coward Creek  AT Baker Road",
    "CCGT2": "Cypress Creek 6 AT Grant Road",
    "CCUT2": "New Caney 1NNE - Caney Creek",
    "CCWT2": "Chiger Creek  AT Windsong Lane",
    "CCXT2": "Woodbranch 3NW - Caney Creek",
    "CDFT2": "Dixie Farm Road",
    "CEFT2": "Huffman 2NE - Cedar Bayou",
    "CEWT2": "Friendswood 1SSE - Cowarts Creek",
    "CFKT2": "West Fork San Jacinto 4.2 S Conroe",
    "CFMT2": "Barrett 7ESE - Cedar Bayou",
    "CGFT2": "Friendswood 2SE - Chigger Creek",
    "CHET2": "TRINITY 10 E",
    "CIFT2": "Cypress Creek  AT Inverness Forest",
    "CKNT2": "CONROE",
    "CLCT2": "Clear Creek  AT Seabrook",
    "CLDT2": "East Fork San Jacinto 1.2 W Cleveland",
    "CNBT2": "Clear Creek In Nassau Bay",
    "COWT2": "Bridge City 2NW - Cow Bayou",
    "CPBT2": "Bunker Hill Village 2NE - Briar Branch",
    "CPGT2": "CLEVELAND 2 S",
    "CPTT2": "CARPENTERS BAYOU @ I-10",
    "CPWT2": "CYPRESS CREEK @ Cypresswood",
    "CUYT2": "Pearland 5WNW - Country Place",
    "CVST2": "Missouri City 1NE - Willow",
    "CWIT2": "Pearland 4S - Coward Creek",
    "CWYT2": "Moss Bluff 3NW - CWA Canal",
    "DEHT2": "Cole Creek  AT Deihl Road",
    "DFMT2": "Dickinson 3SW - Dickinson Bayou",
    "DICT2": "Dickinson - Bayou at Hwy 3",
    "DITT2": "Santa Fe 4NW - Ditch 6",
    "DIXT2": "Webster 6NW - Clear Creek",
    "DMHT2": "GUM GULLY AT DIAMOND HEAD",
    "DRST2": "Sienna Plantation 4NW - Drainage Ditch",
    "DTBT2": "Missouri City 2SSE - Ditch B1",
    "DULT2": "Oyster Creek 3 ESE Sugarland",
    "DWYT2": "Sabine River 0.5 N Deweyville",
    "EFMT2": "Plum Grove 1W - EF San Jacinto",
    "ELBT2": "WHITE OAK BAYOU",
    "EVDT2": "Neches River 3 S Evadale",
    "FADT2": "MCFADDEN",
    "FHBT2": "FRED HARTMAN BRIDGE",
    "FHNT2": "JERSEY VILLAGE",
    "FLPT2": "BIG ISLAND SLOUGH AT FAIRMONT PARKWAY",
    "FNDT2": "FRIENDSWOOD",
    "GABT2": "Garners Bayou 4 AT Beltway 8",
    "GAPT2": "GARNERS BAYOU @ RANKIN ROAD",
    "GBHT2": "Greens Bayou 10 AT U.S. Hwy 59",
    "GBLT2": "Greens Bayou  AT Ley Road",
    "GCBT2": "Goose Creek  AT Baytown",
    "GCGT2": "Greens Bayou  AT Cutten Road",
    "GJCT2": "Greens Bayou 2 NE Normandy Road",
    "GLAT2": "Aldine 2SW - P138",
    "GMBT2": "League City 4E - Gum Bayou",
    "GMKT2": "McNair 1E - Goose Creek",
    "GNTT2": "Clear Creek  AT Greentee Avenue",
    "GOOT2": "Goose Creek  AT Old State Highway 146",
    "GRFT2": "Chateau Woods 2NW - Grograns Mill",
    "GSBT2": "Missouri City 5SW - Gates",
    "GSST2": "Brays Bayou  AT Gessner Drive",
    "GTDT2": "HOUMONT PARK",
    "GUMT2": "Barcliff 2SW - Gum Bayou",
    "GVCT2": "GALVESTON CAUSEWAY",
    "GWAT2": "GREENS BAYOU AT BELTWAY 8",
    "HABT2": "Halls Bayou  AT Jensen Drive",
    "HBGT2": "Mont Belvieu 2SE - Hackberry Gully",
    "HBMT2": "Brays Bayou  AT South Main Street",
    "HBST2": "Holcolmbe",
    "HBTT2": "MOUNT HOUSTON",
    "HCCT2": "Clear Creek  AT Friendswood at FM 528",
    "HCDT2": "Cedar Bayou 6 AT Crosby",
    "HCFT2": "Houston 6NW - Brookhollow",
    "HCPT2": "Max Road",
    "HCST2": "Sims Bayou  AT Hiram Clarke Street",
    "HFFT2": "Luce Bayou 6 NE Huffman",
    "HGBT2": "Greens Bayou 14 N Knobcrest Street",
    "HGHT2": "Pearland 6W - Hickory Slough",
    "HGTT2": "White Oak Bayou  AT Heights Boulevard",
    "HILT2": "High Island 8NNW",
    "HMCT2": "BRAYS BAYOU AT RICE AVE",
    "HMMT2": "West Fork San Jacinto 2.5 N Humble",
    "HPBT2": "Horsepen Bayou  AT Clear Lake City",
    "HPOT2": "Buffalo Bayou 4 AT Turning Basin",
    "HRMT2": "HARMON CREEK",
    "HRST2": "BRAYS BAYOU AT ALIEF",
    "HSIT2": "Sims Bayou  AT Telephone Road",
    "HSJT2": "San Jacinto River 4 N Lake Houston",
    "HSMT2": "Mykawa Drive",
    "HTGT2": "Hunting Bayou  AT Loop 610 East",
    "HUFT2": "Huffman 1NE",
    "JAIT2": "Hamshire 3NE - Craigen Road",
    "JBAT2": "West Port Arthur 2WNW - Taylors Bayou",
    "JBET2": "Fannett 3WNW - NRCS Beehive",
    "JBOT2": "GIWW @ SALT BAYOU OUTFALL",
    "JCHT2": "China 6S - Ditch 800",
    "JDLT2": "Beaumont 4NW - Dowdel Basin",
    "JGAT2": "Fannett 1NE - Green Acres",
    "JGTT2": "Beaumont 5W - Gulf Terrace",
    "JHHT2": "West Port Aurthur 6W - Hebert Heirs Marsh",
    "JKJT2": "KEITH LAKE @ HIGHWAY 87 AT JUNIORS",
    "JLBT2": "Beaumont 5NNE - LNVA Saltwater",
    "JLDT2": "Cheek 3NW - Lawhon Detention",
    "JLGT2": "China 4SE - Lawhon Road",
    "JMDT2": "Hamshire 8SSW - Mayhaw Bayou",
    "JNDT2": "Fannett 8SE - Needmore Div",
    "JNET2": "Sabine Pass 17W - Needmore Div",
    "JNWT2": "Fannett 6SE - Taylors Bayou",
    "JOWT2": "Beaumont 2NNW - Old Walmart Basin",
    "JPAT2": "Port Acres 6SW - Marsh Ditch",
    "JPRT2": "Hamshire 9SE - Willow Slough",
    "JPTT2": "China 5S - Pine Tree Ditch",
    "JSDT2": "Beaumont 2SSW - South 11th Detention",
    "JSFT2": "Beaumont 4 NW - Soccer Fields",
    "JSGT2": "Beaumont 4 NW - Folsom",
    "JTPT2": "Beaumont 5SW - Tyrrell Park",
    "JTWT2": "TRAM RD. @ WALKER DITCH 1000",
    "JVLT2": "White Oak Bayou  AT Jersey Village",
    "JWRT2": "Jasper",
    "JWST2": "Beaumont 3NW - Wellington Screw Gates",
    "JXAT2": "CATTLE WALK @ DITCH 550",
    "JYAT2": "EASTEX FREEWAY @ DITCH 001",
    "JYBT2": "BEAUMONT YACHT CLUB @ NECHES RIVER",
    "JYCT2": "MOORE RD. DETENTION POND",
    "JYDT2": "TAYLOR BAYOU @ NAVIGATION DISTRICT GATES",
    "JYFT2": "GIWW @ S. H. 87 BRIDGE",
    "JYGT2": "TAYLOR BAYOU @ S.H. 73 BRIDGE",
    "JYHT2": "BOONDOCKS RD.  @  TAYLORS BAYOU SOUTH FO",
    "JYIT2": "HILLEBRANDT BAYOU @ HILLEBRANDT RD BRIDG",
    "JYJT2": "BATISTE CREEK @ S.H. 770",
    "JYKT2": "Pine Island Bayou  AT BATSON 2 ENE",
    "JYLT2": "Little Pine Island Bayou  AT THICKET 4 SE",
    "JYMT2": "Black Creek  AT SOUR LAKE 8 NNE",
    "JYNT2": "Pine Island Bayou  AT BEVIL OAKS 1 SW",
    "JYOT2": "BEST RD @ L.N.V.A. PUMP STATION",
    "JYPT2": "STAR LAKE @ GIWW",
    "JYQT2": "MAHAW BAYOU @ WILBER RD",
    "JYRT2": "MAHAW BAYOU @ BRUSH ISLAND ROAD",
    "JYST2": "MAHAW BAYOU @ ENGLIN RD",
    "JYTT2": "EAST LANE @ DITCH 200",
    "JYUT2": "RIDGEWOOD RETIREMENT CENTER",
    "JYVT2": "LAUREL AND EASTEX FREEWAY @ DITCH 116",
    "JYWT2": "EAST LUCAS @DITCH 002",
    "JYXT2": "SPINDLETOP BAYOU @ STATE HIGHWAY 124 BRI",
    "JYYT2": "SABINE RANCH @ DITCH 550",
    "JYZT2": "LABELLE RANCH PROPERTY @ DITCH 552-A",
    "JZAT2": "COTTON CREEK @ GRAYBURG ROAD",
    "JZBT2": "Pine Island Bayou  AT NOME 4 NE",
    "JZCT2": "FOREST TRAIL @ DITCH 1202",
    "JZDT2": "TRAM ROAD @ DITCH 1002",
    "JZET2": "FOLSOM ROAD @ HILLEBRANDT BAYOU",
    "JZFT2": "GLADYS AVE @ HILLEBRANT BAYOU DITCH 100",
    "JZGT2": "WASHINGTON BLVD @ CALDWELL CUTOFF",
    "JZHT2": "SH 124 @ HILLEBRANDT BAYOU",
    "JZIT2": "SOUTH 8TH ST @ DITCH 110",
    "JZJT2": "HIGHLAND AVE @ DITCH 104",
    "JZKT2": "PRUTZMAN RD @ DITCH AMELIA C/O",
    "JZLT2": "LANDIS DRIVE @ DITCH 202B",
    "JZMT2": "WALDEN ROAD @ DITCH 202",
    "JZNT2": "FRINT ROAD @ WILLOW MARSH BAYOU",
    "JZOT2": "KIDD ROAD @ DITCH 406B",
    "JZPT2": "LNVA CHEEK CANAL @ DITCH 407 (GREEN ACRE",
    "JZQT2": "LABELLE ROAD @ PEVITOT BAYOU",
    "JZRT2": "PLANT ROAD @ DITCH 903",
    "JZTT2": "SOUTH CHINA R.D @ DITCH 608",
    "JZUT2": "S. PINE ISLAND RD @ DITCH 607",
    "JZVT2": "STATE HWY 365 @ GREEN POND GULLY",
    "JZWT2": "F.M. 1406 @ NORTH FORK TAYLOR BAYOU",
    "JZYT2": "LABELLE ROAD @ TAYLOR BAYOU",
    "KCCT2": "KINGWOOD",
    "KEET2": "Keegans Bayou 16 SW Keegan Road",
    "KNFT2": "Kenefick 4W",
    "KORT2": "KOHRVILLE",
    "KOUT2": "Village Creek 4 NE Kountze",
    "KRBT2": "KIRBYVILLE RAWS",
    "KYKT2": "Cypress Creek 6 W Kuykendahl Road",
    "LAUT2": "Sugar Land 4SE - Lakes of Austin Park",
    "LCTT2": "West Fork of the San Jacinto 7.4 W Lake Conroe",
    "LDTT2": "League City 1WSW - Landing Ditch",
    "LEDT2": "Pearland 3SE - Leclaire Ditch",
    "LGCT2": "Clear Creek  AT League City",
    "LGTT2": "Missouri City 2SE - Lexington",
    "LHBT2": "Hunting Bayou  AT Houston",
    "LHFT2": "Atascocita 4ENE - Lake Houston",
    "LIVT2": "Long King Creek 2 W Livingston",
    "LKWT2": "Hunting Bayou  AT Lockwood Drive",
    "LPNT2": "Arcola 2NW - Long Point Creek",
    "LPOT2": "LITTLE CEDAR BAYOU AT 8TH STREET",
    "LSHT2": "LUCE BAYOU AT FM 2100",
    "LSIT2": "Missouri City 1E - Cangelosi",
    "LSKT2": "ONALASKA 6 NE",
    "LTST2": "La Porte 2NW - Lateral at Sens Road",
    "LUCT2": "Macedonia 5SW - Luce Bayou",
    "LVDT2": "Trinity River 7 SW Lake Livingston",
    "LVJT2": "LITTLE VINCE BAYOU AT JACKSON",
    "LVPT2": "LITTLE VINCE BAYOU AT BELTWAY 8",
    "LWCT2": "Willow Creek 4 E Tomball",
    "LWDT2": "Brays Bayou  AT Lawndale Avenue",
    "LWOT2": "Little White Oak Bayou  AT Tidwell",
    "MART2": "SH 288 AND MACGREGOR",
    "MBBT2": "League City 3WSW - Magnolia Creek",
    "MBYT2": "Iowa Colony 4NE - Mustang Bayou",
    "MCFT2": "Marys Creek  AT Melodywood Drive",
    "MCPT2": "Fm 1128",
    "MCVT2": "Cow Bayou 2 SW Mauriceville",
    "MDYT2": "FM 529 AND US 290 NR JERSEY VILLAGE",
    "MGBT2": "MIDDLE BAYOU GENOA RED BLUFF DR.",
    "MGOT2": "SH 288 @ MCGOWEN",
    "MHPT2": "Greens Bayou  AT East Mount Houston Parkway",
    "MLHT2": "Longherridge",
    "MLKT2": "Sims Bayou  AT Martin Luther King Boulevard",
    "MOKT2": "MOSES LK TIDE GAGE",
    "MSBT2": "PEARLAND",
    "MUBT2": "Iowa Colony 4N - Mustang Bayou",
    "MVDT2": "Veteran&#039;S Drive",
    "MYKT2": "CLEAR CREEK AT MYKAWA STREET NEAR PEARLA",
    "NCCT2": "Big Cow Creek  AT Newton",
    "NCET2": "East Fork San Jacinto 5.5 E New Caney",
    "NEYT2": "Conroe 7ENE - Caney Creek",
    "NFGT2": "Westfield 4SW - NF Greens Bayou",
    "NMDT2": "IH-10 @ NORMANDY",
    "NRDT2": "Missouri City 2SE - North Ditch",
    "NSGT2": "Sugar Land 3E - E Sugar Creek Ditch",
    "OCDT2": "Sugar Land 4SE - Dulles Ave",
    "OCIT2": "Alvin 4N - Old Chigger Creek",
    "OLBT2": "Sugar Land 3SE - Lexington Blvd",
    "ORET2": "ORANGE",
    "ORFT2": "Orangefield 3NW - Cow Bayou",
    "ORNT2": "Sabine River 1.5 NE Orange",
    "PBST2": "Panther Branch 3 NW Sawdust Road",
    "PBWT2": "The Woodlands - Panther Branch",
    "PCFT2": "SJRA Lake Conroe Weir Flow",
    "PCHT2": "Maynard 6S - Peach Creek",
    "PCJT2": "PIERCE JUNCTION",
    "PECT2": "New Caney 3E - Peach Creek",
    "PGRT2": "Panther Branch  AT Gosling Road",
    "POET2": "Porter 7NW - WF San Jacinto River",
    "PPCT2": "Pearland 2NNE - Clear Creek",
    "PPTT2": "Buffalo Bayou  AT Piney Point Village",
    "PTKT2": "Deer Park - Patricks Bayou",
    "PVLT2": "PATTON VILLAGE",
    "RBST2": "League City 1NE - Robinson Bayou",
    "RCGT2": "Rummel Creek",
    "RDOT2": "Iowa Colony 1E - Rodeo Palms",
    "RIOT2": "SAN JACINTO RIVER NEAR RIO VILLA",
    "RLPT2": "RELIANT PARK",
    "RMBT2": "Deer Park 3SSW - Armand Bayou",
    "RMYT2": "Trinity River 1.9 S Romayor",
    "RRKT2": "Keegans Bayou  AT Roark Road",
    "RROT2": "Alvin 4N - Resort Park Ditch",
    "RVRT2": "Trinity River  AT Riverside",
    "SBRT2": "IH 10 @ SILBER",
    "SCKT2": "HUFSMITH",
    "SCPT2": "Deer Park 3NE - Juan Seguin Park",
    "SDAT2": "Caney Creek 8 W Splendora",
    "SERT2": "Conroe 7WSW - Lake Creek",
    "SFUT2": "Missouri City 2SSW - Stafford Run",
    "SGBT2": "Sugar Land 3ESE - Siphon B",
    "SHLT2": "San Jacinto River 1.5 E Sheldon",
    "SHOT2": "IH 45 S HOV @ DWT TERMINUS",
    "SHTT2": "Fm 2978",
    "SIPT2": "Little Cypress - Sabine River",
    "SIRT2": "Sienna Plantatation 3WSW - Brookshire Creek",
    "SJRT2": "Woodlock 1SE - San Jacinto River",
    "SLKT2": "BRAYS BAYOU AT STELLA LINK",
    "SMGT2": "Mont Belvieu 1SSW - Smith Gully",
    "SMWT2": "Harris Gully  AT Medical Center",
    "SOLT2": "Pine Island Bayou 5.1 SE Sour Lake",
    "SPDT2": "Peach Creek 6.4 SW Splendora",
    "SPNT2": "Spring Creek 1.1 NE Spring",
    "STUT2": "Cypress Creek 6 AT Stuebner-Airline Road",
    "TBAT2": "Colmesneil",
    "TBLT2": "Neches River  AT Steinhagen Lake",
    "TCAT2": "Missouri City 1SE - Cangelosi",
    "TKNT2": "Cleveland 3SE - Tarkington Bayou",
    "TLNT2": "TAYLOR LAKE @ NASA ROAD 1",
    "TNST2": "6922 OLD KATY RD",
    "TPRT2": "Seabrook 3NNW - Taylor Bayou",
    "TPST2": "Texas City Pump Station  AT Texas City",
    "TUKT2": "TURKEY CK AT FM 1959",
    "TXDT2": "Missouri City 1ENE",
    "UCCT2": "AT Pearland",
    "VCBT2": "Vince Bayou  AT Ellaine Street",
    "VCVT2": "HIGHLAND HEIGHTS",
    "VDPT2": "Cr 128",
    "VGBT2": "HIGHLAND HEIGHTS",
    "VINT2": "Pasadena 3SSE - Vince Bayou",
    "WABT2": "White Oak Bayou 11 NW Alabonson Road",
    "WCCT2": "Iowa Colony 1W - WF Chocolate Bayou",
    "WCLT2": "Cloverfield Road",
    "WCPT2": "WOLF CREEK PARK",
    "WCVT2": "NR CHARLOTTE",
    "WDLT2": "UPPER KICKAPOO CREEK",
    "WFDT2": "Cypress Creek 1 AT Westfield",
    "WHLT2": "Meyerland 2SW - Willow Water",
    "WIBT2": "Maynard 2NE - Winters Bayou",
    "WIGT2": "Missouri City 1N - Willow",
    "WJPT2": "Shenandoah 2NW - WJPA",
    "WLIT2": "WILLIS",
    "WOBT2": "Little White Oak Bayou  AT Trimble Street",
    "WOJT2": "SATSUMA",
    "WOTT2": "ROSSLYN",
    "WPOT2": "WHITE OAK BAYOU @ PINEMONT",
    "WPWT2": "Egypt 3S - Woodlands Parkway",
    "WRRT2": "SOUTHERN ROUGH FTS",
    "WSBT2": "Buffalo Bayou  AT West Belt Drive",
    "WSHT2": "IH 10 EAST OF WASHINGTON AVE",
    "WTDT2": "POINT BLANK 6 N",
    "WVLT2": "WOODVILLE RAWS",
    "WWHT2": "MEYERLAND",
    "WWYT2": "Hunters Creek Village 2E - Buffalo Bayou",
    "WYBT2": "WYSER BLUFF",
}

# Deliberately excludes PCIRGZZ -- confirmed live that SHEF code is an
# *accumulator* (running total since some undocumented reset point,
# not an hourly rate), which produced physically impossible values
# (100-200+ "inches/hour") when treated the same as the others. The
# four kept here are genuine period/rate codes -- verified clean
# (0.0-0.01 in/hr) across a live sample with zero outliers, unlike
# PCIRGZZ. Also deliberately excludes the HG*-prefixed stage/level
# codes this same network reports at many stations -- those measure
# creek/bayou water level in feet, not rainfall, and aren't comparable
# to the ASOS heavy-rain threshold at all.
DCP_PRECIP_VARS = ["PPHRRZZ", "PPERRZZ", "PPHRGZZ", "PPURRZZ"]
DCP_CHUNK_SIZE = 90  # verified: larger chunks risk CGI URL-length limits

# KFDM WeatherNet -- a genuine standalone local weather-station network
# (schools, fire departments, ranches; not flood/drainage infrastructure),
# per instruction to prefer non-creek/river station sources. Free, no
# signup, no API key -- confirmed live via the station's own public KML
# feed (dc3.weatheractive.org), which returns already-computed values
# (Temperature/Humidity/Dewpoint/Wind/Pressure/Rainfall), not raw codes
# needing interpretation. 93 stations verified live across the corridor
# (Beaumont, Port Arthur, Orange, Winnie, Jasper, Silsbee, Lumberton,
# Vidor, Nederland, Groves, Sabine Pass, Kountze, Woodville, and more).
KFDM_WEATHERNET_STATIONS = {
    "Baptist Hospital": "Baptist Hospital",
    "Bayou Din": "Bayou Din",
    "Jefferson Energy": "Jefferson Energy",
    "LCM HS": "LCM HS",
    "Bridge City": "Bridge City",
    "Buna": "Buna",
    "China": "China",
    "County Home": "County Home",
    "Winnie": "Winnie",
    "Evadale": "Evadale",
    "Port Acres ES": "Port Acres ES",
    "Doucette": "Doucette",
    "Fred": "Fred",
    "Fannett": "Fannett",
    "High Island": "High Island",
    "Hillcrest": "Hillcrest",
    "Idylwild": "Idylwild",
    "Jasper": "Jasper",
    "Chester": "Chester",
    "Kountze": "Kountze",
    "Silsbee": "Silsbee",
    "Babe Zaharias": "Babe Zaharias",
    "Lumberton M.S.": "Lumberton M.S.",
    "Newton": "Newton",
    "Nome": "Nome",
    "Pinehurst": "Pinehurst",
    "Port Of PA": "Port Of PA",
    "Dominion Ranch": "Dominion Ranch",
    "Roy Guess": "Roy Guess",
    "Sabine Pass": "Sabine Pass",
    "Shangri La": "Shangri La",
    "Sour Lake": "Sour Lake",
    "The Big Store": "The Big Store",
    "Vidor": "Vidor",
    "Warren": "Warren",
    "Saratoga": "Saratoga",
    "Wildwood": "Wildwood",
    "Woodville": "Woodville",
    "Ford Park": "Ford Park",
    "Sea Rim": "Sea Rim",
    "Gilbert Adams": "Gilbert Adams",
    "Port Neches": "Port Neches",
    "Mauriceville": "Mauriceville",
    "Port Bolivar": "Port Bolivar",
    "Sam Houston": "Sam Houston",
    "Colmesneil": "Colmesneil",
    "Gentz Ranch": "Gentz Ranch",
    "Southern Nursery": "Southern Nursery",
    "Spurger": "Spurger",
    "Devers": "Devers",
    "Tyrell Park": "Tyrell Park",
    "Diamond D Ranch": "Diamond D Ranch",
    "Deweyville HS": "Deweyville HS",
    "Beech Grove VFD": "Beech Grove VFD",
    "Vincent MS": "Vincent MS",
    "Sallie Curtis ES": "Sallie Curtis ES",
    "Orangefield ISD": "Orangefield ISD",
    "Brookeland ISD": "Brookeland ISD",
    "All Saints Episcopal School": "All Saints Episcopal School",
    "Pietzsch-MacArthur School": "Pietzsch-MacArthur School",
    "Rayburn Realty": "Rayburn Realty",
    "Magnolia Springs": "Magnolia Springs",
    "Groves Fire Dept": "Groves Fire Dept",
    "Nederland Fire Dept": "Nederland Fire Dept",
    "Burkeville VFD": "Burkeville VFD",
    "Beaumont Country Club": "Beaumont Country Club",
    "Rocking Y Ranch": "Rocking Y Ranch",
    "C.O. Wilson Middle School": "C.O. Wilson Middle School",
    "Gator Country": "Gator Country",
    "Barbers Hill ISD": "Barbers Hill ISD",
    "West Orange Police Dept": "West Orange Police Dept",
    "Sabine River Authority": "Sabine River Authority",
    "Bevil Oaks": "Bevil Oaks",
    "Hamshire-Fannett HS": "Hamshire-Fannett HS",
    "Trout Creek VFD": "Trout Creek VFD",
    "Votaw-Thicket VFD": "Votaw-Thicket VFD",
    "Gulf Coast Bug Zappers": "Gulf Coast Bug Zappers",
    "Roganville VFD": "Roganville VFD",
    "Naskila Casino": "Naskila Casino",
    "Utopia Ranch": "Utopia Ranch",
    "Ivanhoe City Hall": "Ivanhoe City Hall",
    "Crystal Beach": "Crystal Beach",
    "Mauriceville-1 mile north": "Mauriceville-1 mile north",
    "Lakeview-3 miles east": "Lakeview-3 miles east",
    "Holly Beach": "Holly Beach",
    "R.C.Services": "R.C.Services",
    "Lamar Inst of Technology": "Lamar Inst of Technology",
    "Tolbert Ranch": "Tolbert Ranch",
    "Arceneaux Ranch": "Arceneaux Ranch",
    "Hardin County ESD#6": "Hardin County ESD#6",
    "Spindletop Boomtown": "Spindletop Boomtown",
    "Dam B VFD": "Dam B VFD",
    "Kirbyville VFD": "Kirbyville VFD",
}
KFDM_KML_URL = "http://dc3.weatheractive.org/KFDM/KML/DATA/temperature.kml"

# Hard physical sanity ceiling applied to EVERY heavy-rain hazard check,
# regardless of source -- per instruction, after the DCP layer's SHEF
# accumulator field alerted "30-40+ inches in the last hour" (physically
# impossible; the actual world record is ~12in/hr). This alone would
# have completely blocked that incident. Any single-station reading
# above this is treated as a data/parsing artifact, not real weather,
# and is silently dropped rather than alerted on.
PHOUR_SANITY_MAX_IN = 6.0

# Same principle, sized for a running DAILY total instead of an hourly
# rate (KFDM's "Rainfall" field -- see fetch_kfdm_obs()). Historic
# single-day extremes in this region during major hurricanes have
# approached this range; anything beyond it is a data artifact.
RAIN_TODAY_SANITY_MAX_IN = 30.0

# Per instruction: only alert once a station's running total reaches
# 1in, then a fresh alert at every additional inch as a real event
# keeps climbing (1, 2, 3, 4, 5in and beyond), so an ongoing
# significant event keeps getting updates instead of going silent
# after the first one. Each tier is its own hazard key, so
# classify_hazards() only ever alerts once per tier -- and
# process_metar_storm() collapses multiple tiers newly crossed in the
# SAME cycle down to just the highest one, so a fast-rising event (or
# a cold-start where several tiers are already past) never repeats the
# same number on multiple lines.
RAIN_TODAY_TIERS = [float(i) for i in range(1, int(RAIN_TODAY_SANITY_MAX_IN))]

GUST_THRESHOLD_MPH = 40  # per instruction: alert on any gust/high-wind reading over 40mph
HEAVY_RAIN_HOURLY_IN = 0.5

KT_TO_MPH = 1.15078


def _http_get_bytes(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "metar-storm-pipeline/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _fetch_with_retries_bytes(url, label):
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            data = _http_get_bytes(url)
            if data:
                return data
            print(f"[{label}] Attempt {attempt}: empty response from {url}")
        except Exception as e:
            print(f"[{label}] Attempt {attempt} failed ({url}): {e}")
        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_DELAY_SEC)
    return None


def fetch_corridor_conditions():
    """Live, observed (not forecast) current conditions at the
    corridor's ASOS stations, via IEM's currents.json -- confirmed
    live this covers all target stations directly by station code,
    no need to fetch a whole state network."""
    stations = ",".join(CORRIDOR_STATIONS.keys())
    url = f"https://mesonet.agron.iastate.edu/api/1/currents.json?station={stations}"
    data = _fetch_with_retries_bytes(url, "METARStorm:currents")
    if not data:
        return None
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return parsed.get("data")


def _fetch_hads_chunk(station_ids, start, end):
    stations_param = ",".join(station_ids)
    url = (
        "https://mesonet.agron.iastate.edu/cgi-bin/request/hads.py?"
        f"network=TX_DCP&stations={stations_param}"
        f"&year1={start.year}&month1={start.month}&day1={start.day}"
        f"&year2={end.year}&month2={end.month}&day2={end.day}"
        f"&vars={','.join(DCP_PRECIP_VARS)}&format=comma"
    )
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "metar-storm-pipeline/1.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                text = resp.read().decode("utf-8", errors="replace")
                if text.strip():
                    return text
        except Exception as e:
            print(f"[DCP fetch] chunk attempt {attempt} failed: {e}")
        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_DELAY_SEC)
    return None


def fetch_dcp_precip_obs():
    """Live hourly precip from the corridor's NOAA/USGS HADS/DCP rain
    gauges, chunked to stay under the CGI endpoint's URL-length limit.
    Queried over a short recent window (not just 'today') so a gauge
    that just posted its top-of-the-hour reading a few minutes ago is
    never missed at the edge of a UTC day boundary. Returns obs in the
    same shape as the ASOS obs (station/wxcodes/gust/phour) so
    classify_hazards() can process both uniformly -- wxcodes and gust
    are always absent here since these gauges have no present-weather
    or wind sensors, so only the heavy-rain hazard can ever fire for
    them."""
    now_utc = datetime.now(timezone.utc)
    start = now_utc - timedelta(hours=3)
    station_ids = list(DCP_CORRIDOR_STATIONS.keys())
    chunks = [station_ids[i:i + DCP_CHUNK_SIZE] for i in range(0, len(station_ids), DCP_CHUNK_SIZE)]

    latest_by_station = {}
    for chunk in chunks:
        text = _fetch_hads_chunk(chunk, start, now_utc)
        if not text:
            continue
        try:
            reader = csv.DictReader(io.StringIO(text))
            for row in reader:
                sid = row.get("station")
                valid = row.get("utc_valid")
                if not sid or not valid:
                    continue
                for var in DCP_PRECIP_VARS:
                    raw = row.get(var)
                    if raw in (None, ""):
                        continue
                    try:
                        value = float(raw)
                    except ValueError:
                        continue
                    if value < 0:
                        continue
                    prev = latest_by_station.get(sid)
                    if prev is None or valid > prev[0]:
                        latest_by_station[sid] = (valid, value)
                    break  # first matching var for this row wins, per station's own reporting convention
        except Exception as e:
            print(f"[DCP fetch] parse failed for a chunk (non-fatal): {e}")

    return [
        {"station": sid, "wxcodes": "", "gust": None, "phour": value}
        for sid, (_, value) in latest_by_station.items()
    ]


KFDM_PLACEMARK_RE = re.compile(r"<Placemark>(.*?)</Placemark>", re.S)
KFDM_NAME_RE = re.compile(r"<name>([^<]+)</name>")
KFDM_RAIN_RE = re.compile(r"Rainfall:\s*([\-0-9.]+)")
KFDM_WIND_RE = re.compile(r"Wind:\s*([0-9.]+)")
KFDM_TIME_RE = re.compile(r"<B>([\d/]+\s+[\d:]+\s*[AP]M\[[A-Za-z]+\])</B>")


def fetch_kfdm_obs():
    """Live conditions from the KFDM WeatherNet station network --
    already-computed values (not raw codes needing interpretation), per
    the KML feed's own description text. Non-fatal on failure, matching
    every other observation source here."""
    data = _fetch_with_retries_bytes(KFDM_KML_URL, "KFDM:kml")
    if not data:
        return []
    try:
        text = data.decode("latin-1")
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")

    obs = []
    for block in KFDM_PLACEMARK_RE.findall(text):
        name_m = KFDM_NAME_RE.search(block)
        if not name_m:
            continue
        name = name_m.group(1).strip()
        if name not in KFDM_WEATHERNET_STATIONS:
            continue  # skip the map-legend placemarks (Station Logo, Caption)

        # KFDM's "Rainfall" field is the running total since local
        # midnight, NOT an hourly rate -- confirmed against a station's
        # own detail page, which lists it separately from "Rainfall
        # Rate (in/hr)" (a field this feed doesn't expose). Deliberately
        # NOT put in "phour" -- that field means "in the last hour"
        # elsewhere in this file, and mixing the two meanings under one
        # key is exactly the kind of ambiguity that caused the DCP
        # incident. Kept as its own "rain_today" field with its own
        # tiered classify_hazards() handling instead.
        rain_today = None
        rain_m = KFDM_RAIN_RE.search(block)
        if rain_m:
            try:
                v = float(rain_m.group(1))
                if v >= 0:
                    rain_today = v
            except ValueError:
                pass

        wind_mph = None
        wind_m = KFDM_WIND_RE.search(block)
        if wind_m:
            try:
                wind_mph = float(wind_m.group(1))
            except ValueError:
                pass

        obs_time = None
        time_m = KFDM_TIME_RE.search(block)
        if time_m:
            obs_time = time_m.group(1).strip()

        obs.append({"station": name, "wxcodes": "", "gust": None, "wind_mph": wind_mph, "rain_today": rain_today, "obs_time": obs_time})

    return obs


# --- Weather Underground / IBM Weather Company PWS network ----------
# Per instruction: hundreds more real stations across the same
# corridor, using the user's own registered-station API key (never
# committed -- read from the environment, matching every other
# credential in this repo). Confirmed live 2026-08-09: a 9x9 grid scan
# around Beaumont finds 500+ real unique stations within 95 miles in
# well under a second (concurrent), and fetching all of their current
# conditions concurrently takes ~5-6s -- fast enough to do fresh, in
# full, every single poll cycle rather than caching a station list.
WU_CENTER_LAT = 30.0802  # Beaumont, TX
WU_CENTER_LON = -94.1266
WU_RADIUS_MILES = 95
WU_GRID_STEP = 0.45
WU_GRID_OFFSETS = [-2.0, -1.5, -1.0, -0.5, 0, 0.5, 1.0, 1.5, 2.0]
WU_MAX_WORKERS = 20
WU_STALE_MAX_AGE_MIN = 20  # matches KFDM -- WU PWS reports nearly real-time (confirmed live: median age 0.3min)
WU_RAIN_TODAY_SANITY_MAX_IN = 30.0  # same ceiling as KFDM's rain_today -- see RAIN_TODAY_SANITY_MAX_IN comment
WU_GUST_SANITY_MAX_MPH = 150.0  # backyard PWS hardware can glitch; a US wind-gust record is ~253mph (tornado), but a
# plain PWS anemometer reporting anywhere near that is a sensor fault, not real weather -- discard rather than alert.


def _wu_haversine_miles(lat1, lon1, lat2, lon2):
    R = 3958.8
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _wu_get_nearby(lat, lon, api_key):
    url = f"https://api.weather.com/v3/location/near?geocode={lat},{lon}&product=pws&format=json&apiKey={api_key}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "metar-storm-pipeline/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []
    loc = data.get("location") or {}
    ids = loc.get("stationId") or []
    lats = loc.get("latitude") or []
    lons = loc.get("longitude") or []
    return list(zip(ids, lats, lons))


def wu_discover_stations(api_key):
    """Concurrent 9x9 grid scan around Beaumont -- see WU_GRID_* consts.
    Returns {station_id: (lat, lon, distance_mi)} for every unique
    station within WU_RADIUS_MILES, deduped across overlapping grid
    cells."""
    grid_points = [
        (WU_CENTER_LAT + dlat * WU_GRID_STEP, WU_CENTER_LON + dlon * WU_GRID_STEP)
        for dlat in WU_GRID_OFFSETS for dlon in WU_GRID_OFFSETS
    ]
    stations = {}
    with ThreadPoolExecutor(max_workers=WU_MAX_WORKERS) as ex:
        futures = [ex.submit(_wu_get_nearby, lat, lon, api_key) for lat, lon in grid_points]
        for fut in as_completed(futures):
            for sid, lat_s, lon_s in fut.result():
                if not sid or sid in stations or lat_s is None or lon_s is None:
                    continue
                dist = _wu_haversine_miles(WU_CENTER_LAT, WU_CENTER_LON, lat_s, lon_s)
                if dist <= WU_RADIUS_MILES:
                    stations[sid] = (lat_s, lon_s, round(dist, 1))
    return stations


def _wu_get_current(station_id, api_key):
    url = (
        f"https://api.weather.com/v2/pws/observations/current"
        f"?stationId={station_id}&format=json&units=e&apiKey={api_key}&numericPrecision=decimal"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "metar-storm-pipeline/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    obs_list = data.get("observations") or []
    return obs_list[0] if obs_list else None


def fetch_wu_pws_obs():
    """Hundreds of real Weather Underground PWS stations across the
    corridor, discovered fresh every cycle (see wu_discover_stations)
    and fetched concurrently. Non-fatal on any failure -- an unset key,
    a network blip, or WU being down just means this cycle runs
    without this source, same as every other observation source here.

    qcStatus 0 means WU's own QC explicitly FAILED that reading --
    those are dropped outright, not just deprioritized (same spirit as
    the DCP incident: don't trust a source's own known-bad flag away).
    Physical sanity ceilings on gust/rain_today guard against the
    backyard-hardware equivalent of that incident (a glitched sensor
    reporting a wildly implausible spike)."""
    api_key = os.environ.get("WU_API_KEY")
    if not api_key:
        return []
    try:
        stations = wu_discover_stations(api_key)
    except Exception as e:
        print(f"[WU PWS] Station discovery failed this cycle (non-fatal): {e}")
        return []
    if not stations:
        return []

    obs = []
    with ThreadPoolExecutor(max_workers=WU_MAX_WORKERS) as ex:
        futures = {ex.submit(_wu_get_current, sid, api_key): sid for sid in stations}
        for fut in as_completed(futures):
            sid = futures[fut]
            r = fut.result()
            if not r:
                continue
            if r.get("qcStatus") == 0:
                continue  # WU's own QC failed this reading -- don't trust it

            _, _, dist_mi = stations[sid]
            imperial = r.get("imperial") or {}

            gust = imperial.get("windGust")
            if gust is not None and gust > WU_GUST_SANITY_MAX_MPH:
                print(f"[WU PWS sanity check] Discarding implausible gust={gust}mph at {sid} -- sensor fault, not real weather.")
                gust = None

            rain_today = imperial.get("precipTotal")
            if rain_today is not None and rain_today > WU_RAIN_TODAY_SANITY_MAX_IN:
                print(f"[WU PWS sanity check] Discarding implausible rain_today={rain_today}in at {sid} -- sensor fault, not real weather.")
                rain_today = None

            neighborhood = r.get("neighborhood") or sid
            label = f"{neighborhood} -- {dist_mi}mi from Beaumont ({sid})"

            obs_time_display = None
            obs_time_local = r.get("obsTimeLocal")
            if obs_time_local:
                try:
                    dt = datetime.strptime(obs_time_local, "%Y-%m-%d %H:%M:%S")
                    obs_time_display = dt.strftime("%-m/%-d %-I:%M%p")
                except ValueError:
                    obs_time_display = obs_time_local

            obs.append({
                "station": label,
                "wxcodes": "",
                "gust": None,  # WU's "gust" already arrives in mph, not knots -- goes in wind_mph, same as KFDM
                "wind_mph": gust,
                "rain_today": rain_today,
                "obs_time": obs_time_display,
                "wu_utc_valid": r.get("obsTimeUtc"),
            })
    return obs


# Per instruction: a "storm reported" alert older than this isn't
# useful -- the storm's likely moved on by the time it's read. Applied
# to every source before hazard classification. Two different
# ceilings, not one: ASOS/METAR only issues routine reports roughly
# hourly by design (confirmed live -- normal ages ran 5-58min across
# the corridor), so a 20min cutoff would silence it almost entirely,
# not just filter genuinely stale reports. KFDM updates every few
# minutes, so it can and should hold to the tighter number.
ASOS_STALE_MAX_AGE_MIN = 65
KFDM_STALE_MAX_AGE_MIN = 20


def _parse_kfdm_time(obs_time_str):
    """Parses KFDM's '08/08/2026 4:36PM[CST]' into a Beaumont-local
    aware datetime. The bracketed timezone abbreviation is ignored --
    KFDM's station clock is always Beaumont local time regardless of
    whether it prints CST or CDT."""
    if not obs_time_str:
        return None
    clean = obs_time_str.split("[")[0].strip()
    try:
        naive = datetime.strptime(clean, "%m/%d/%Y %I:%M%p")
    except ValueError:
        return None
    return naive.replace(tzinfo=BEAUMONT_TZ)


def is_stale(ob, now_utc):
    """True if this observation is older than its source's staleness
    ceiling. Checks whichever timestamp field the source actually
    provided (ASOS: utc_valid; KFDM: obs_time; WU PWS: wu_utc_valid --
    kept separate from ASOS's utc_valid despite the identical ISO
    format, since WU needs KFDM's tighter cadence-appropriate ceiling,
    not ASOS's much more lenient one). No timestamp at all -- don't
    block on it rather than silently dropping every observation from a
    source that doesn't carry one."""
    wu_utc_valid = ob.get("wu_utc_valid")
    if wu_utc_valid:
        try:
            obs_dt = datetime.strptime(wu_utc_valid, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            return False
        return (now_utc - obs_dt).total_seconds() / 60 > WU_STALE_MAX_AGE_MIN

    utc_valid = ob.get("utc_valid")
    if utc_valid:
        try:
            obs_dt = datetime.strptime(utc_valid, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            return False
        return (now_utc - obs_dt).total_seconds() / 60 > ASOS_STALE_MAX_AGE_MIN

    obs_time = ob.get("obs_time")
    if obs_time:
        obs_dt = _parse_kfdm_time(obs_time)
        if obs_dt is None:
            return False
        now_local = now_utc.astimezone(BEAUMONT_TZ)
        return (now_local - obs_dt).total_seconds() / 60 > KFDM_STALE_MAX_AGE_MIN

    return False


def classify_hazards(ob):
    """Returns a dict of {hazard_key: description} for whatever real,
    observed hazards this specific station report shows right now.
    Ordered roughly by severity -- funnel/tornado first."""
    hazards = {}
    wxcodes = (ob.get("wxcodes") or "").upper()

    if "FC" in wxcodes.split() or "+FC" in wxcodes:
        hazards["funnel"] = "Funnel cloud / tornado reported"
    if "TS" in wxcodes:
        hazards["thunderstorm"] = "Thunderstorm reported"
    if "GR" in wxcodes:
        hazards["hail"] = "Hail reported"

    # ASOS gust arrives in knots; KFDM wind arrives already in mph --
    # both compared against the same mph threshold per instruction
    # ("any gusts over 40mph"), converting only where needed.
    gust_kt = ob.get("gust")
    if gust_kt is not None:
        gust_mph = gust_kt * KT_TO_MPH
        if gust_mph >= GUST_THRESHOLD_MPH:
            hazards["gust"] = f"Wind gust to {round(gust_mph)} mph observed"

    obs_time = ob.get("obs_time")
    time_suffix = f" (as of {obs_time})" if obs_time else ""

    wind_mph = ob.get("wind_mph")
    if wind_mph is not None and wind_mph >= GUST_THRESHOLD_MPH:
        hazards["gust"] = f"High wind -- {round(wind_mph)} mph observed{time_suffix}"

    phour = ob.get("phour")
    if phour is not None and phour >= HEAVY_RAIN_HOURLY_IN:
        # Hard sanity ceiling, applied regardless of source -- see
        # PHOUR_SANITY_MAX_IN comment. This is what the DCP incident
        # needed and didn't have.
        if phour > PHOUR_SANITY_MAX_IN:
            print(f"[sanity check] Discarding implausible phour={phour}in at this station -- data artifact, not real rain, not alerting.")
        else:
            hazards["heavy_rain"] = f"Heavy rain observed -- {phour}\" in the last hour"

    # KFDM's running-total-since-midnight field, per instruction: alert
    # when it crosses 0.5in, and again -- separately, not suppressed by
    # the first alert -- when it crosses 1.5in. A single hazard key
    # would only fire once at 0.5in and then go silent for the rest of
    # the day since the total never drops back down; one key PER TIER
    # lets each threshold announce itself independently the first time
    # it's crossed.
    rain_today = ob.get("rain_today")
    if rain_today is not None:
        if rain_today > RAIN_TODAY_SANITY_MAX_IN:
            print(f"[sanity check] Discarding implausible rain_today={rain_today}in at this station -- data artifact, not real rain, not alerting.")
        else:
            for tier in RAIN_TODAY_TIERS:
                if rain_today >= tier:
                    hazards[f"rain_today_{tier}"] = f"Rain total climbing -- now {rain_today}\"{time_suffix}"

    return hazards


HAZARD_EMOJI = {
    "funnel": "🌪️",
    "thunderstorm": "⛈️",
    "hail": "🧊",
    "gust": "💨",
    "heavy_rain": "💧",
}


def hazard_emoji(hazard_key):
    """HAZARD_EMOJI covers the fixed hazard types; rain_today_X has one
    key per tier (0.5, 1.0, 2.0, ... -- see RAIN_TODAY_TIERS), so it's
    handled by magnitude here instead of one dict entry per tier."""
    if hazard_key.startswith("rain_today_"):
        tier = float(hazard_key.removeprefix("rain_today_"))
        return "🌊" if tier >= 3.0 else "💧"
    return HAZARD_EMOJI.get(hazard_key, "⚠️")


def build_message(new_hazards_by_station):
    now_local = datetime.now(BEAUMONT_TZ)
    lines = [
        "🚨 <b>Real Storm Watch</b> -- station observations (not model)",
        f"📅 {now_local.strftime('%A, %b %-d %I:%M %p').replace(' 0', ' ')}",
        "",
    ]
    for station, hazards in new_hazards_by_station.items():
        name = CORRIDOR_STATIONS.get(station) or DCP_CORRIDOR_STATIONS.get(station)
        # KFDM stations have no separate short code -- name and station
        # key are the same string, so skip the redundant "(X (X))".
        header = f"{name} ({station})" if name else station
        lines.append(f"<b>{header}</b>")
        for hazard_key, desc in hazards.items():
            lines.append(f"{hazard_emoji(hazard_key)} {desc}")
        lines.append("")
    return "\n".join(lines).rstrip()


def telegram_configured():
    return bool(os.environ.get("NWS_TELEGRAM_BOT_TOKEN")) and bool(os.environ.get("NWS_TELEGRAM_CHAT_ID"))


def send_telegram(text):
    bot_token = os.environ["NWS_TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["NWS_TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    # Telegram caps a single message at 4096 chars -- with the DCP
    # layer, a widespread rain event can newly trip heavy-rain on
    # dozens of gauges in one cycle, so this can no longer be assumed
    # to always fit in one message.
    max_len = 4000
    chunks = [text[i:i + max_len] for i in range(0, len(text), max_len)] or [text]
    for idx, chunk in enumerate(chunks, 1):
        payload = json.dumps({"chat_id": chat_id, "text": chunk, "parse_mode": "HTML"}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        last_err = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
                    if result.get("ok"):
                        break
                    last_err = result.get("description", "Unknown Telegram error")
            except Exception as e:
                last_err = str(e)
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_DELAY_SEC)
        else:
            raise RuntimeError(f"Telegram send failed after {MAX_ATTEMPTS} attempts on chunk {idx}: {last_err}")


def deliver(text):
    if not telegram_configured():
        print("Telegram not configured -- skipping.")
        raise RuntimeError("NWS Telegram not configured")
    send_telegram(text)


def send_failure_alert(context, error):
    try:
        deliver(f"[metar-storm-pipeline error] {context}: {error}")
    except Exception as e:
        print(f"Could not even send the failure alert: {e}", file=sys.stderr)


def load_state():
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _collapse_rain_tiers(newly_appeared):
    """If a station's total jumped enough in one cycle to newly cross
    several rain_today tiers at once (a fast-rising event, or a
    cold-start where multiple tiers were already past), keep only the
    single highest one -- they'd otherwise repeat the identical total
    on multiple lines, which reads as spam rather than new info."""
    rain_keys = [k for k in newly_appeared if k.startswith("rain_today_")]
    if len(rain_keys) <= 1:
        return newly_appeared
    highest = max(rain_keys, key=lambda k: float(k.removeprefix("rain_today_")))
    return {k: v for k, v in newly_appeared.items() if not k.startswith("rain_today_") or k == highest}


def process_metar_storm(state):
    asos_obs = fetch_corridor_conditions()
    if not asos_obs:
        print("ASOS corridor conditions unavailable this cycle (non-fatal).")
        asos_obs = []

    # DCP rain-gauge layer DISABLED per instruction, 2026-08-07 -- during
    # a real rain event it sent "30-40+ inches in the last hour" alerts
    # across ~40 stations simultaneously (physically impossible; world
    # record is ~12in/hr). The dry-weather-only sample used to vet
    # DCP_PRECIP_VARS wasn't enough to catch this -- at least one of
    # those "period" SHEF codes behaves like an accumulator (or resets
    # on an irregular/event basis, not a fixed hourly window) under
    # real rain, the same failure mode PCIRGZZ already showed. Left in
    # place but not called, pending a proper fix validated against an
    # actual rain event before ever re-enabling.
    dcp_obs = []

    try:
        kfdm_obs = fetch_kfdm_obs()
    except Exception as e:
        print(f"KFDM WeatherNet fetch failed this cycle (non-fatal): {e}")
        kfdm_obs = []
    print(f"[KFDM] {len(kfdm_obs)}/{len(KFDM_WEATHERNET_STATIONS)} stations reported this cycle.")

    try:
        wu_obs = fetch_wu_pws_obs()
    except Exception as e:
        print(f"WU PWS fetch failed this cycle (non-fatal): {e}")
        wu_obs = []
    print(f"[WU PWS] {len(wu_obs)} stations reported this cycle.")

    obs = asos_obs + dcp_obs + kfdm_obs + wu_obs
    if not obs:
        print("No observations from either source this cycle -- skipping.")
        return

    active_hazards = state.get("active_hazards", {})
    # Start from the PREVIOUS cycle's memory, not empty -- a station that
    # simply doesn't show up in this cycle's fetch (a transient KFDM
    # network/parse hiccup, not real weather) must not lose its recorded
    # hazards, or the next time it reports the exact same still-elevated
    # rain total, the diff below would see it as "new" again and re-send
    # an alert for rain that happened hours ago (confirmed live, 2026-08-08:
    # "Rocking Y Ranch" vanished from one cycle's KFDM fetch, dropped out of
    # active_hazards, then reappeared next cycle and re-triggered rain_today_1.0
    # even though its total hadn't moved and it hadn't rained in hours).
    new_active_hazards = dict(active_hazards)
    new_hazards_by_station = {}
    now_utc = datetime.now(timezone.utc)

    for ob in obs:
        station = ob.get("station")
        if not station:
            continue
        if is_stale(ob, now_utc):
            print(f"[{station}] Skipping -- data too old to be useful for a live storm alert.")
            continue
        hazards = classify_hazards(ob)
        # This station actually reported fresh data this cycle, so its
        # memory is fully replaced by what's true right now (clears
        # resolved momentary hazards like gust/thunderstorm correctly).
        if hazards:
            new_active_hazards[station] = list(hazards.keys())
        else:
            new_active_hazards.pop(station, None)
        prev_hazards = set(active_hazards.get(station, []))
        newly_appeared = {k: v for k, v in hazards.items() if k not in prev_hazards}
        newly_appeared = _collapse_rain_tiers(newly_appeared)
        if newly_appeared:
            new_hazards_by_station[station] = newly_appeared
            print(f"[{station}] New hazard(s): {list(newly_appeared.keys())}")
        elif hazards:
            print(f"[{station}] Hazard(s) ongoing, already alerted: {list(hazards.keys())}")

    state["active_hazards"] = new_active_hazards
    save_state(state)

    if not new_hazards_by_station:
        print("No newly-appearing real hazards this cycle -- not sending.")
        return

    message = build_message(new_hazards_by_station)
    print(f"Sending -- {message}")
    try:
        deliver(message)
    except Exception as e:
        send_failure_alert("Real storm watch delivery", str(e))
        return
    print("Sent successfully.")


def main():
    state = load_state()
    try:
        process_metar_storm(state)
    except Exception as e:
        print(f"Unexpected error (non-fatal): {e}")


if __name__ == "__main__":
    main()

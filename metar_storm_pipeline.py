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
import os
import sys
import time
import urllib.error
import urllib.request
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

GUST_THRESHOLD_KT = 40  # ~46 mph -- a real, useful heads-up level, below the official 50kt severe criterion
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

    gust = ob.get("gust")
    if gust is not None and gust >= GUST_THRESHOLD_KT:
        mph = round(gust * KT_TO_MPH)
        hazards["gust"] = f"Wind gust to {mph} mph observed"

    phour = ob.get("phour")
    if phour is not None and phour >= HEAVY_RAIN_HOURLY_IN:
        hazards["heavy_rain"] = f"Heavy rain observed -- {phour}\" in the last hour"

    return hazards


HAZARD_EMOJI = {
    "funnel": "🌪️",
    "thunderstorm": "⛈️",
    "hail": "🧊",
    "gust": "💨",
    "heavy_rain": "💧",
}


def build_message(new_hazards_by_station):
    now_local = datetime.now(BEAUMONT_TZ)
    lines = [
        "🚨 <b>Real Storm Watch</b> -- station observations (not model)",
        f"📅 {now_local.strftime('%A, %b %-d %I:%M %p').replace(' 0', ' ')} (Beaumont time)",
        "",
    ]
    for station, hazards in new_hazards_by_station.items():
        name = CORRIDOR_STATIONS.get(station) or DCP_CORRIDOR_STATIONS.get(station, station)
        lines.append(f"<b>{name} ({station})</b>")
        for hazard_key, desc in hazards.items():
            lines.append(f"{HAZARD_EMOJI.get(hazard_key, '⚠️')} {desc}")
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

    obs = asos_obs + dcp_obs
    if not obs:
        print("No observations from either source this cycle -- skipping.")
        return

    active_hazards = state.get("active_hazards", {})
    new_active_hazards = {}
    new_hazards_by_station = {}

    for ob in obs:
        station = ob.get("station")
        if not station:
            continue
        hazards = classify_hazards(ob)
        if hazards:
            new_active_hazards[station] = list(hazards.keys())
        prev_hazards = set(active_hazards.get(station, []))
        newly_appeared = {k: v for k, v in hazards.items() if k not in prev_hazards}
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

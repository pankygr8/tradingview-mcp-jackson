"""
screener.py — NSE All-Time-High screener.

Rules
─────
• ATH watchlist  → stocks that made a NEW all-time high in the last 90 calendar days
• 50 % ATH alert → stocks whose current price is within ±5 % of half their ATH price
                   (i.e. approaching or sitting at the 50 % retracement level)

Designed to be called from app.py via start_scan() / get_status().
All heavy lifting runs in a background daemon thread.
Results are cached per-universe for CACHE_TTL seconds.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

import yfinance as yf

# ── Universe definitions ──────────────────────────────────────────────────────

# Full name map: yfinance symbol → display name
NSE_STOCKS: dict[str, str] = {
    "360ONE.NS": "360 ONE WAM",
    "3MINDIA.NS": "3M India",
    "AADHARHFC.NS": "Aadhar Housing Finance",
    "AARTIIND.NS": "Aarti Industries",
    "AAVAS.NS": "Aavas Financiers",
    "ABB.NS": "ABB India",
    "ABBOTINDIA.NS": "Abbott India",
    "ABCAPITAL.NS": "Aditya Birla Capital",
    "ABDL.NS": "Allied Blenders and Distillers",
    "ABFRL.NS": "Aditya Birla Fashion and Retail",
    "ABLBL.NS": "Aditya Birla Lifestyle Brands",
    "ABREL.NS": "Aditya Birla Real Estate",
    "ABSLAMC.NS": "Aditya Birla Sun Life AMC",
    "ACC.NS": "ACC",
    "ACE.NS": "Action Construction Equipment",
    "ACMESOLAR.NS": "ACME Solar Holdings",
    "ACUTAAS.NS": "Acutaas Chemicals",
    "ADANIENSOL.NS": "Adani Energy Solutions",
    "ADANIENT.NS": "Adani Enterprises",
    "ADANIGREEN.NS": "Adani Green Energy",
    "ADANIPORTS.NS": "Adani Ports and Special Economic Zone",
    "ADANIPOWER.NS": "Adani Power",
    "AEGISLOG.NS": "Aegis Logistics",
    "AEGISVOPAK.NS": "Aegis Vopak Terminals",
    "AFCONS.NS": "Afcons Infrastructure",
    "AFFLE.NS": "Affle 3i",
    "AIAENG.NS": "AIA Engineering",
    "AIIL.NS": "Authum Investment & Infrastructure",
    "AJANTPHARM.NS": "Ajanta Pharmaceuticals",
    "ALKEM.NS": "Alkem Laboratories",
    "AMBER.NS": "Amber Enterprises India",
    "AMBUJACEM.NS": "Ambuja Cements",
    "ANANDRATHI.NS": "Anand Rathi Wealth",
    "ANANTRAJ.NS": "Anant Raj",
    "ANGELONE.NS": "Angel One",
    "ANTHEM.NS": "Anthem Biosciences",
    "ANURAS.NS": "Anupam Rasayan India",
    "APARINDS.NS": "Apar Industries",
    "APLAPOLLO.NS": "APL Apollo Tubes",
    "APOLLOHOSP.NS": "Apollo Hospitals Enterprise",
    "APOLLOTYRE.NS": "Apollo Tyres",
    "APTUS.NS": "Aptus Value Housing Finance India",
    "ARE&M.NS": "Amara Raja Energy & Mobility",
    "ASAHIINDIA.NS": "Asahi India Glass",
    "ASHOKLEY.NS": "Ashok Leyland",
    "ASIANPAINT.NS": "Asian Paints",
    "ASTERDM.NS": "Aster DM Quality Care",
    "ASTRAL.NS": "Astral",
    "ATGL.NS": "Adani Total Gas",
    "ATHERENERG.NS": "Ather Energy",
    "ATUL.NS": "Atul",
    "AUBANK.NS": "AU Small Finance Bank",
    "AUROPHARMA.NS": "Aurobindo Pharma",
    "AWL.NS": "AWL Agri Business",
    "AXISBANK.NS": "Axis Bank",
    "BAJAJ-AUTO.NS": "Bajaj Auto",
    "BAJAJFINSV.NS": "Bajaj Finserv",
    "BAJAJHFL.NS": "Bajaj Housing Finance",
    "BAJAJHLDNG.NS": "Bajaj Holdings & Investment",
    "BAJFINANCE.NS": "Bajaj Finance",
    "BALKRISIND.NS": "Balkrishna Industries",
    "BALRAMCHIN.NS": "Balrampur Chini Mills",
    "BANDHANBNK.NS": "Bandhan Bank",
    "BANKBARODA.NS": "Bank of Baroda",
    "BANKINDIA.NS": "Bank of India",
    "BATAINDIA.NS": "Bata India",
    "BAYERCROP.NS": "Bayer Cropscience",
    "BBTC.NS": "Bombay Burmah Trading Corporation",
    "BDL.NS": "Bharat Dynamics",
    "BEL.NS": "Bharat Electronics",
    "BELRISE.NS": "Belrise Industries",
    "BEML.NS": "BEML",
    "BERGEPAINT.NS": "Berger Paints India",
    "BHARATFORG.NS": "Bharat Forge",
    "BHARTIARTL.NS": "Bharti Airtel",
    "BHARTIHEXA.NS": "Bharti Hexacom",
    "BHEL.NS": "Bharat Heavy Electricals",
    "BIKAJI.NS": "Bikaji Foods International",
    "BIOCON.NS": "Biocon",
    "BLS.NS": "BLS International Services",
    "BLUEDART.NS": "Blue Dart Express",
    "BLUEJET.NS": "Blue Jet Healthcare",
    "BLUESTARCO.NS": "Blue Star",
    "BOSCHLTD.NS": "Bosch",
    "BPCL.NS": "Bharat Petroleum Corporation",
    "BRIGADE.NS": "Brigade Enterprises",
    "BRITANNIA.NS": "Britannia Industries",
    "BSE.NS": "BSE",
    "BSOFT.NS": "Birlasoft",
    "CAMS.NS": "Computer Age Management Services",
    "CANBK.NS": "Canara Bank",
    "CANFINHOME.NS": "Can Fin Homes",
    "CANHLIFE.NS": "Canara HSBC Life Insurance Company",
    "CAPLIPOINT.NS": "Caplin Point Laboratories",
    "CARBORUNIV.NS": "Carborundum Universal",
    "CARTRADE.NS": "Cartrade Tech",
    "CASTROLIND.NS": "Castrol India",
    "CCL.NS": "CCL Products (I)",
    "CDSL.NS": "Central Depository Services (India)",
    "CEATLTD.NS": "Ceat",
    "CEMPRO.NS": "Cemindia Projects",
    "CENTRALBK.NS": "Central Bank of India",
    "CESC.NS": "CESC",
    "CGCL.NS": "Capri Global Capital",
    "CGPOWER.NS": "CG Power and Industrial Solutions",
    "CHALET.NS": "Chalet Hotels",
    "CHAMBLFERT.NS": "Chambal Fertilizers & Chemicals",
    "CHENNPETRO.NS": "Chennai Petroleum Corporation",
    "CHOICEIN.NS": "Choice International",
    "CHOLAFIN.NS": "Cholamandalam Investment and Finance Company",
    "CHOLAHLDNG.NS": "Cholamandalam Financial Holdings",
    "CIEINDIA.NS": "CIE Automotive India",
    "CIPLA.NS": "Cipla",
    "CLEAN.NS": "Clean Science and Technology",
    "COALINDIA.NS": "Coal India",
    "COCHINSHIP.NS": "Cochin Shipyard",
    "COFORGE.NS": "Coforge",
    "COHANCE.NS": "Cohance Lifesciences",
    "COLPAL.NS": "Colgate Palmolive (India)",
    "CONCOR.NS": "Container Corporation of India",
    "CONCORDBIO.NS": "Concord Biotech",
    "COROMANDEL.NS": "Coromandel International",
    "CPPLUS.NS": "Aditya Infotech",
    "CRAFTSMAN.NS": "Craftsman Automation",
    "CREDITACC.NS": "CreditAccess Grameen",
    "CRISIL.NS": "CRISIL",
    "CROMPTON.NS": "Crompton Greaves Consumer Electricals",
    "CUB.NS": "City Union Bank",
    "CUMMINSIND.NS": "Cummins India",
    "CYIENT.NS": "Cyient",
    "DABUR.NS": "Dabur India",
    "DALBHARAT.NS": "Dalmia Bharat",
    "DATAPATTNS.NS": "Data Patterns (India)",
    "DCMSHRIRAM.NS": "DCM Shriram",
    "DEEPAKFERT.NS": "Deepak Fertilisers & Petrochemicals Corp.",
    "DEEPAKNTR.NS": "Deepak Nitrite",
    "DELHIVERY.NS": "Delhivery",
    "DEVYANI.NS": "Devyani International",
    "DIVISLAB.NS": "Divi's Laboratories",
    "DIXON.NS": "Dixon Technologies (India)",
    "DLF.NS": "DLF",
    "DMART.NS": "Avenue Supermarts",
    "DOMS.NS": "DOMS Industries",
    "DRREDDY.NS": "Dr. Reddy's Laboratories",
    "ECLERX.NS": "eClerx Services",
    "EICHERMOT.NS": "Eicher Motors",
    "EIDPARRY.NS": "E.I.D. Parry (India)",
    "EIHOTEL.NS": "EIH",
    "ELECON.NS": "Elecon Engineering Co.",
    "ELGIEQUIP.NS": "Elgi Equipments",
    "EMAMILTD.NS": "Emami",
    "EMCURE.NS": "Emcure Pharmaceuticals",
    "EMMVEE.NS": "Emmvee Photovoltaic Power",
    "ENDURANCE.NS": "Endurance Technologies",
    "ENGINERSIN.NS": "Engineers India",
    "ENRIN.NS": "Siemens Energy India",
    "ERIS.NS": "Eris Lifesciences",
    "ESCORTS.NS": "Escorts Kubota",
    "ETERNAL.NS": "Eternal",
    "EXIDEIND.NS": "Exide Industries",
    "FACT.NS": "Fertilisers and Chemicals Travancore",
    "FEDERALBNK.NS": "Federal Bank",
    "FINCABLES.NS": "Finolex Cables",
    "FIRSTCRY.NS": "Brainbees Solutions",
    "FIVESTAR.NS": "Five-Star Business Finance",
    "FLUOROCHEM.NS": "Gujarat Fluorochemicals",
    "FORCEMOT.NS": "Force Motors",
    "FORTIS.NS": "Fortis Healthcare",
    "FSL.NS": "Firstsource Solutions",
    "GABRIEL.NS": "Gabriel India",
    "GAIL.NS": "GAIL (India)",
    "GALLANTT.NS": "Gallantt Ispat",
    "GESHIP.NS": "Great Eastern Shipping Co.",
    "GICRE.NS": "General Insurance Corporation of India",
    "GILLETTE.NS": "Gillette India",
    "GLAND.NS": "Gland Pharma",
    "GLAXO.NS": "Glaxosmithkline Pharmaceuticals",
    "GLENMARK.NS": "Glenmark Pharmaceuticals",
    "GMDCLTD.NS": "Gujarat Mineral Development Corporation",
    "GMRAIRPORT.NS": "GMR Airports",
    "GODFRYPHLP.NS": "Godfrey Phillips India",
    "GODIGIT.NS": "Go Digit General Insurance",
    "GODREJCP.NS": "Godrej Consumer Products",
    "GODREJIND.NS": "Godrej Industries",
    "GODREJPROP.NS": "Godrej Properties",
    "GPIL.NS": "Godawari Power & Ispat",
    "GRANULES.NS": "Granules India",
    "GRAPHITE.NS": "Graphite India",
    "GRASIM.NS": "Grasim Industries",
    "GRAVITA.NS": "Gravita India",
    "GROWW.NS": "Billionbrains Garage Ventures",
    "GRSE.NS": "Garden Reach Shipbuilders & Engineers",
    "GVT&D.NS": "GE Vernova T&D India",
    "HAL.NS": "Hindustan Aeronautics",
    "HAVELLS.NS": "Havells India",
    "HBLENGINE.NS": "HBL Engineering",
    "HCLTECH.NS": "HCL Technologies",
    "HDBFS.NS": "HDB Financial Services",
    "HDFCAMC.NS": "HDFC Asset Management Company",
    "HDFCBANK.NS": "HDFC Bank",
    "HDFCLIFE.NS": "HDFC Life Insurance Company",
    "HEG.NS": "H.E.G.",
    "HEROMOTOCO.NS": "Hero MotoCorp",
    "HEXT.NS": "Hexaware Technologies",
    "HFCL.NS": "HFCL",
    "HINDALCO.NS": "Hindalco Industries",
    "HINDCOPPER.NS": "Hindustan Copper",
    "HINDPETRO.NS": "Hindustan Petroleum Corporation",
    "HINDUNILVR.NS": "Hindustan Unilever",
    "HINDZINC.NS": "Hindustan Zinc",
    "HOMEFIRST.NS": "Home First Finance Company India",
    "HONASA.NS": "Honasa Consumer",
    "HONAUT.NS": "Honeywell Automation India",
    "HSCL.NS": "Himadri Speciality Chemical",
    "HUDCO.NS": "Housing & Urban Development Corporation",
    "HYUNDAI.NS": "Hyundai Motor India",
    "ICICIAMC.NS": "ICICI Prudential Asset Management Company",
    "ICICIBANK.NS": "ICICI Bank",
    "ICICIGI.NS": "ICICI Lombard General Insurance Company",
    "ICICIPRULI.NS": "ICICI Prudential Life Insurance Company",
    "IDBI.NS": "IDBI Bank",
    "IDEA.NS": "Vodafone Idea",
    "IDFCFIRSTB.NS": "IDFC First Bank",
    "IEX.NS": "Indian Energy Exchange",
    "IFCI.NS": "IFCI",
    "IGIL.NS": "International Gemological Institute",
    "IGL.NS": "Indraprastha Gas",
    "IIFL.NS": "IIFL Finance",
    "IKS.NS": "Inventurus Knowledge Solutions",
    "INDGN.NS": "Indegene",
    "INDHOTEL.NS": "Indian Hotels Co.",
    "INDIACEM.NS": "India Cements",
    "INDIAMART.NS": "Indiamart Intermesh",
    "INDIANB.NS": "Indian Bank",
    "INDIGO.NS": "InterGlobe Aviation",
    "INDUSINDBK.NS": "IndusInd Bank",
    "INDUSTOWER.NS": "Indus Towers",
    "INFY.NS": "Infosys",
    "INOXWIND.NS": "Inox Wind",
    "INTELLECT.NS": "Intellect Design Arena",
    "IOB.NS": "Indian Overseas Bank",
    "IOC.NS": "Indian Oil Corporation",
    "IPCALAB.NS": "Ipca Laboratories",
    "IRB.NS": "IRB Infrastructure Developers",
    "IRCON.NS": "IRCON International",
    "IRCTC.NS": "Indian Railway Catering And Tourism Corporation",
    "IREDA.NS": "Indian Renewable Energy Development Agency",
    "IRFC.NS": "Indian Railway Finance Corporation",
    "ITC.NS": "ITC",
    "ITCHOTELS.NS": "ITC Hotels",
    "ITI.NS": "ITI",
    "J&KBANK.NS": "Jammu & Kashmir Bank",
    "JAINREC.NS": "Jain Resource Recycling",
    "JBMA.NS": "JBM Auto",
    "JINDALSAW.NS": "Jindal Saw",
    "JINDALSTEL.NS": "Jindal Steel",
    "JIOFIN.NS": "Jio Financial Services",
    "JKCEMENT.NS": "J.K. Cement",
    "JKTYRE.NS": "JK Tyre & Industries",
    "JMFINANCIL.NS": "JM Financial",
    "JPPOWER.NS": "Jaiprakash Power Ventures",
    "JSL.NS": "Jindal Stainless",
    "JSWCEMENT.NS": "JSW Cement",
    "JSWDULUX.NS": "JSW Dulux",
    "JSWENERGY.NS": "JSW Energy",
    "JSWINFRA.NS": "JSW Infrastructure",
    "JSWSTEEL.NS": "JSW Steel",
    "JUBLFOOD.NS": "Jubilant Foodworks",
    "JUBLINGREA.NS": "Jubilant Ingrevia",
    "JUBLPHARMA.NS": "Jubilant Pharmova",
    "JWL.NS": "Jupiter Wagons",
    "JYOTICNC.NS": "Jyoti CNC Automation",
    "KAJARIACER.NS": "Kajaria Ceramics",
    "KALYANKJIL.NS": "Kalyan Jewellers India",
    "KARURVYSYA.NS": "Karur Vysya Bank",
    "KAYNES.NS": "Kaynes Technology India",
    "KEC.NS": "Kec International",
    "KEI.NS": "KEI Industries",
    "KFINTECH.NS": "Kfin Technologies",
    "KIMS.NS": "Krishna Institute of Medical Sciences",
    "KIRLOSENG.NS": "Kirloskar Oil Eng",
    "KOTAKBANK.NS": "Kotak Mahindra Bank",
    "KPIL.NS": "Kalpataru Projects International",
    "KPITTECH.NS": "KPIT Technologies",
    "KPRMILL.NS": "K.P.R. Mill",
    "LALPATHLAB.NS": "Dr. Lal Path Labs",
    "LATENTVIEW.NS": "Latent View Analytics",
    "LAURUSLABS.NS": "Laurus Labs",
    "LEMONTREE.NS": "Lemon Tree Hotels",
    "LENSKART.NS": "Lenskart Solutions",
    "LGEINDIA.NS": "LG Electronics India",
    "LICHSGFIN.NS": "LIC Housing Finance",
    "LICI.NS": "Life Insurance Corporation of India",
    "LINDEINDIA.NS": "Linde India",
    "LLOYDSME.NS": "Lloyds Metals And Energy",
    "LODHA.NS": "Lodha Developers",
    "LT.NS": "Larsen & Toubro",
    "LTF.NS": "L&T Finance",
    "LTFOODS.NS": "LT Foods",
    "LTM.NS": "LTM",
    "LTTS.NS": "L&T Technology Services",
    "LUPIN.NS": "Lupin",
    "M&M.NS": "Mahindra & Mahindra",
    "M&MFIN.NS": "Mahindra & Mahindra Financial Services",
    "MAHABANK.NS": "Bank of Maharashtra",
    "MANAPPURAM.NS": "Manappuram Finance",
    "MANKIND.NS": "Mankind Pharma",
    "MAPMYINDIA.NS": "C.E. Info Systems",
    "MARICO.NS": "Marico",
    "MARUTI.NS": "Maruti Suzuki India",
    "MAXHEALTH.NS": "Max Healthcare Institute",
    "MAZDOCK.NS": "Mazagoan Dock Shipbuilders",
    "MCX.NS": "Multi Commodity Exchange of India",
    "MEDANTA.NS": "Global Health",
    "MEESHO.NS": "Meesho",
    "MFSL.NS": "Max Financial Services",
    "MGL.NS": "Mahanagar Gas",
    "MINDACORP.NS": "Minda Corporation",
    "MMTC.NS": "MMTC",
    "MOTHERSON.NS": "Samvardhana Motherson International",
    "MOTILALOFS.NS": "Motilal Oswal Financial Services",
    "MPHASIS.NS": "MphasiS",
    "MRF.NS": "MRF",
    "MRPL.NS": "Mangalore Refinery & Petrochemicals",
    "MSUMI.NS": "Motherson Sumi Wiring India",
    "MUTHOOTFIN.NS": "Muthoot Finance",
    "NAM-INDIA.NS": "Nippon Life India Asset Management",
    "NATCOPHARM.NS": "NATCO Pharma",
    "NATIONALUM.NS": "National Aluminium Co.",
    "NAUKRI.NS": "Info Edge (India)",
    "NAVA.NS": "Nava",
    "NAVINFLUOR.NS": "Navin Fluorine International",
    "NBCC.NS": "NBCC (India)",
    "NCC.NS": "NCC",
    "NESTLEIND.NS": "Nestle India",
    "NETWEB.NS": "Netweb Technologies India",
    "NEULANDLAB.NS": "Neuland Laboratories",
    "NEWGEN.NS": "Newgen Software Technologies",
    "NH.NS": "Narayana Hrudayalaya",
    "NHPC.NS": "NHPC",
    "NIACL.NS": "The New India Assurance Company",
    "NIVABUPA.NS": "Niva Bupa Health Insurance Company",
    "NLCINDIA.NS": "NLC India",
    "NMDC.NS": "NMDC",
    "NSLNISP.NS": "NMDC Steel",
    "NTPC.NS": "NTPC",
    "NTPCGREEN.NS": "NTPC Green Energy",
    "NUVAMA.NS": "Nuvama Wealth Management",
    "NUVOCO.NS": "Nuvoco Vistas Corporation",
    "NYKAA.NS": "FSN E-Commerce Ventures",
    "OBEROIRLTY.NS": "Oberoi Realty",
    "OFSS.NS": "Oracle Financial Services Software",
    "OIL.NS": "Oil India",
    "OLAELEC.NS": "Ola Electric Mobility",
    "OLECTRA.NS": "Olectra Greentech",
    "ONESOURCE.NS": "Onesource Specialty Pharma",
    "ONGC.NS": "Oil & Natural Gas Corporation",
    "PAGEIND.NS": "Page Industries",
    "PARADEEP.NS": "Paradeep Phosphates",
    "PATANJALI.NS": "Patanjali Foods",
    "PAYTM.NS": "One 97 Communications",
    "PCBL.NS": "PCBL Chemical",
    "PERSISTENT.NS": "Persistent Systems",
    "PETRONET.NS": "Petronet LNG",
    "PFC.NS": "Power Finance Corporation",
    "PFIZER.NS": "Pfizer",
    "PFOCUS.NS": "Prime Focus",
    "PGEL.NS": "PG Electroplast",
    "PHOENIXLTD.NS": "Phoenix Mills",
    "PIDILITIND.NS": "Pidilite Industries",
    "PIIND.NS": "PI Industries",
    "PINELABS.NS": "Pine Labs",
    "PIRAMALFIN.NS": "Piramal Finance",
    "PNB.NS": "Punjab National Bank",
    "PNBHOUSING.NS": "PNB Housing Finance",
    "POLICYBZR.NS": "PB Fintech",
    "POLYCAB.NS": "Polycab India",
    "POLYMED.NS": "Poly Medicure",
    "POONAWALLA.NS": "Poonawalla Fincorp",
    "POWERGRID.NS": "Power Grid Corporation of India",
    "POWERINDIA.NS": "Hitachi Energy India",
    "PPLPHARMA.NS": "Piramal Pharma",
    "PREMIERENE.NS": "Premier Energies",
    "PRESTIGE.NS": "Prestige Estates Projects",
    "PTCIL.NS": "PTC Industries",
    "PVRINOX.NS": "PVR INOX",
    "PWL.NS": "Physicswallah",
    "RADICO.NS": "Radico Khaitan",
    "RAILTEL.NS": "Railtel Corporation Of India",
    "RAINBOW.NS": "Rainbow Childrens Medicare",
    "RAMCOCEM.NS": "The Ramco Cements",
    "RBLBANK.NS": "RBL Bank",
    "RECLTD.NS": "REC",
    "REDINGTON.NS": "Redington",
    "RELIANCE.NS": "Reliance Industries",
    "RHIM.NS": "RHI MAGNESITA INDIA",
    "RITES.NS": "RITES",
    "RKFORGE.NS": "Ramkrishna Forgings",
    "RPOWER.NS": "Reliance Power",
    "RRKABEL.NS": "R R Kabel",
    "RVNL.NS": "Rail Vikas Nigam",
    "SAGILITY.NS": "Sagility",
    "SAIL.NS": "Steel Authority of India",
    "SAILIFE.NS": "Sai Life Sciences",
    "SAMMAANCAP.NS": "Sammaan Capital",
    "SAPPHIRE.NS": "Sapphire Foods India",
    "SARDAEN.NS": "Sarda Energy and Minerals",
    "SAREGAMA.NS": "Saregama India",
    "SBFC.NS": "SBFC Finance",
    "SBICARD.NS": "SBI Cards and Payment Services",
    "SBILIFE.NS": "SBI Life Insurance Company",
    "SBIN.NS": "State Bank of India",
    "SCHAEFFLER.NS": "Schaeffler India",
    "SCHNEIDER.NS": "Schneider Electric Infrastructure",
    "SCI.NS": "Shipping Corporation of India",
    "SHREECEM.NS": "Shree Cement",
    "SHRIRAMFIN.NS": "Shriram Finance",
    "SHYAMMETL.NS": "Shyam Metalics and Energy",
    "SIEMENS.NS": "Siemens",
    "SIGNATURE.NS": "Signatureglobal (India)",
    "SJVN.NS": "SJVN",
    "SOBHA.NS": "Sobha",
    "SOLARINDS.NS": "Solar Industries India",
    "SONACOMS.NS": "Sona BLW Precision Forgings",
    "SONATSOFTW.NS": "Sonata Software",
    "SPLPETRO.NS": "Supreme Petrochem",
    "SRF.NS": "SRF",
    "STARHEALTH.NS": "Star Health and Allied Insurance Company",
    "SUMICHEM.NS": "Sumitomo Chemical India",
    "SUNDARMFIN.NS": "Sundaram Finance",
    "SUNPHARMA.NS": "Sun Pharmaceutical Industries",
    "SUNTV.NS": "Sun TV Network",
    "SUPREMEIND.NS": "Supreme Industries",
    "SUZLON.NS": "Suzlon Energy",
    "SWANCORP.NS": "Swan Corp",
    "SWIGGY.NS": "Swiggy",
    "SYNGENE.NS": "Syngene International",
    "SYRMA.NS": "Syrma SGS Technology",
    "TARIL.NS": "Transformers And Rectifiers (India)",
    "TATACAP.NS": "Tata Capital",
    "TATACHEM.NS": "Tata Chemicals",
    "TATACOMM.NS": "Tata Communications",
    "TATACONSUM.NS": "Tata Consumer Products",
    "TATAELXSI.NS": "Tata Elxsi",
    "TATAINVEST.NS": "Tata Investment Corporation",
    "TATAPOWER.NS": "Tata Power Co.",
    "TATASTEEL.NS": "Tata Steel",
    "TATATECH.NS": "Tata Technologies",
    "TBOTEK.NS": "TBO Tek",
    "TCS.NS": "Tata Consultancy Services",
    "TECHM.NS": "Tech Mahindra",
    "TECHNOE.NS": "Techno Electric & Engineering Company",
    "TEGA.NS": "Tega Industries",
    "TEJASNET.NS": "Tejas Networks",
    "TENNIND.NS": "Tenneco Clean Air India",
    "THELEELA.NS": "Leela Palaces Hotels & Resorts",
    "THERMAX.NS": "Thermax",
    "TIINDIA.NS": "Tube Investments of India",
    "TIMKEN.NS": "Timken India",
    "TITAGARH.NS": "Titagarh Rail Systems",
    "TITAN.NS": "Titan Company",
    "TMCV.NS": "Tata Motors",
    "TMPV.NS": "Tata Motors Passenger Vehicles",
    "TORNTPHARM.NS": "Torrent Pharmaceuticals",
    "TORNTPOWER.NS": "Torrent Power",
    "TRAVELFOOD.NS": "Travel Food Services",
    "TRENT.NS": "Trent",
    "TRIDENT.NS": "Trident",
    "TRITURBINE.NS": "Triveni Turbine",
    "TTML.NS": "Tata Teleservices (Maharashtra)",
    "TVSMOTOR.NS": "TVS Motor Company",
    "UBL.NS": "United Breweries",
    "UCOBANK.NS": "UCO Bank",
    "ULTRACEMCO.NS": "UltraTech Cement",
    "UNIONBANK.NS": "Union Bank of India",
    "UNITDSPR.NS": "United Spirits",
    "UNOMINDA.NS": "UNO Minda",
    "UPL.NS": "UPL",
    "URBANCO.NS": "Urban Company",
    "USHAMART.NS": "Usha Martin",
    "UTIAMC.NS": "UTI Asset Management Company",
    "VBL.NS": "Varun Beverages",
    "VEDL.NS": "Vedanta",
    "VIJAYA.NS": "Vijaya Diagnostic Centre",
    "VMM.NS": "Vishal Mega Mart",
    "VOLTAS.NS": "Voltas",
    "VTL.NS": "Vardhman Textiles",
    "WAAREEENER.NS": "Waaree Energies",
    "WELCORP.NS": "Welspun Corp",
    "WELSPUNLIV.NS": "Welspun Living",
    "WHIRLPOOL.NS": "Whirlpool of India",
    "WIPRO.NS": "Wipro",
    "WOCKPHARMA.NS": "Wockhardt",
    "YESBANK.NS": "Yes Bank",
    "ZEEL.NS": "Zee Entertainment Enterprises",
    "ZENSARTECH.NS": "Zensar Technolgies",
    "ZENTEC.NS": "Zen Technologies",
    "ZFCVINDIA.NS": "ZF Commercial Vehicle Control Systems India",
    "ZYDUSLIFE.NS": "Zydus Lifesciences",
    "ZYDUSWELL.NS": "Zydus Wellness",
}

# ── NSE FnO universe — actively traded F&O stocks ────────────────────────────
# Stocks listed in NSE derivatives segment (equity F&O)
_FNO_SYMBOLS: list[str] = [
    # ── Nifty 50 (all F&O eligible) ──────────────────────────────────────────
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS",
    "HINDUNILVR.NS", "ITC.NS", "SBIN.NS", "BHARTIARTL.NS", "BAJFINANCE.NS",
    "LT.NS", "WIPRO.NS", "AXISBANK.NS", "ASIANPAINT.NS", "MARUTI.NS",
    "TITAN.NS", "SUNPHARMA.NS", "ULTRACEMCO.NS", "TECHM.NS", "NESTLEIND.NS",
    "HCLTECH.NS", "POWERGRID.NS", "NTPC.NS", "COALINDIA.NS", "ONGC.NS",
    "ADANIENT.NS", "ADANIPORTS.NS", "TMPV.NS", "TMCV.NS", "TATASTEEL.NS", "JSWSTEEL.NS",
    # TATAMOTORS.NS demerged (Oct 2024) into TMPV.NS (passenger vehicles) and
    # TMCV.NS (commercial vehicles) — both are F&O eligible under new tickers.
    "BAJAJFINSV.NS", "BPCL.NS", "CIPLA.NS", "DRREDDY.NS", "DIVISLAB.NS",
    "HEROMOTOCO.NS", "HINDALCO.NS", "INDUSINDBK.NS", "M&M.NS", "EICHERMOT.NS",
    "GRASIM.NS", "APOLLOHOSP.NS", "BRITANNIA.NS", "TATACONSUM.NS", "SHREECEM.NS",
    "SBILIFE.NS", "HDFCLIFE.NS", "KOTAKBANK.NS", "UPL.NS", "BAJAJ-AUTO.NS",
    # ── Nifty Next 50 / large-cap F&O ────────────────────────────────────────
    # BERGEPAINT removed — NOT in NSE F&O
    "ABB.NS", "ADANIGREEN.NS", "AMBUJACEM.NS", "AUROPHARMA.NS", "BANDHANBNK.NS",
    "BIOCON.NS", "BOSCHLTD.NS", "CANBK.NS", "CHOLAFIN.NS", "CONCOR.NS",
    "CUMMINSIND.NS", "DABUR.NS", "DLF.NS", "GAIL.NS", "GODREJCP.NS",
    "GODREJPROP.NS", "HDFCAMC.NS", "ICICIGI.NS", "IDFCFIRSTB.NS", "INDIGO.NS",
    "IOC.NS", "IRCTC.NS", "JUBLFOOD.NS", "LUPIN.NS", "MARICO.NS",
    "MPHASIS.NS", "MUTHOOTFIN.NS", "NAUKRI.NS", "NMDC.NS", "PAGEIND.NS",
    "PFC.NS", "PETRONET.NS", "PNB.NS", "RECLTD.NS", "SAIL.NS",
    "SIEMENS.NS", "SBICARD.NS", "TATACOMM.NS", "TATACHEM.NS", "TATAPOWER.NS",
    "UBL.NS", "VEDL.NS", "ZEEL.NS",
    # ── Midcap / additional F&O stocks ───────────────────────────────────────
    # Removed: CUB (not F&O), DELTACORP (suspended from F&O), GSFC (not F&O),
    #          GNFC (dropped out of Nifty 500 market-cap range)
    "ACC.NS", "AARTIIND.NS", "ABCAPITAL.NS", "ABFRL.NS", "ALKEM.NS",
    "APOLLOTYRE.NS", "ASHOKLEY.NS", "ASTRAL.NS", "ATGL.NS", "AUBANK.NS",
    "BALKRISIND.NS", "BANKBARODA.NS", "BATAINDIA.NS", "BEL.NS", "BHEL.NS",
    "CANFINHOME.NS", "CDSL.NS", "COFORGE.NS", "COLPAL.NS", "CROMPTON.NS",
    "DEEPAKNTR.NS", "DIXON.NS", "ESCORTS.NS", "EXIDEIND.NS", "FEDERALBNK.NS",
    "GMRAIRPORT.NS", "GRANULES.NS", "HAVELLS.NS", "HINDCOPPER.NS",
    "HINDPETRO.NS", "IEX.NS", "IGL.NS", "INDHOTEL.NS", "INDUSTOWER.NS",
    "IPCALAB.NS", "JINDALSTEL.NS", "JKCEMENT.NS", "KPITTECH.NS", "LALPATHLAB.NS",
    "LTF.NS", "LTTS.NS", "MCX.NS", "UNITDSPR.NS", "MFSL.NS",
    "MOTHERSON.NS", "MRF.NS", "NATIONALUM.NS", "NAVINFLUOR.NS", "OBEROIRLTY.NS",
    "OFSS.NS", "PIRAMALFIN.NS", "PERSISTENT.NS", "PIDILITIND.NS", "POLYCAB.NS",
    "PVRINOX.NS", "RAMCOCEM.NS", "SUNTV.NS", "SUPREMEIND.NS",
    "TATAELXSI.NS", "TORNTPHARM.NS", "TORNTPOWER.NS", "TRENT.NS", "TRIDENT.NS",
    "UNIONBANK.NS", "VOLTAS.NS", "ETERNAL.NS", "MANAPPURAM.NS",
    # ── Renames applied vs. previous list: TATAMOTORS→TMPV+TMCV (demerger),
    # GMRINFRA→GMRAIRPORT, LTFH→LTF, MCDOWELL-N→UNITDSPR, PEL→PIRAMALFIN,
    # ZOMATO→ETERNAL. GNFC dropped (no longer in Nifty 500 range).
]

# ── Crypto universe (yfinance format: SYMBOL-USD) ────────────────────────────
CRYPTO_SYMBOLS: dict[str, str] = {
    "BTC-USD":  "Bitcoin",
    "ETH-USD":  "Ethereum",
}

# ── MCX Indian commodities — NYMEX benchmark contracts via yfinance ───────────
# yfinance has no direct MCX India feed; CL=F / NG=F are the international
# reference prices that MCX Crude Oil & Natural Gas contracts are priced against.
MCX_SYMBOLS: dict[str, str] = {
    "CL=F":  "MCX Crude Oil",
    "NG=F":  "MCX Natural Gas",
}

# ── Forex pairs (yfinance format: XXXYYY=X) ───────────────────────────────────
FOREX_SYMBOLS: dict[str, str] = {
    "XAUUSD=X":  "Gold / USD",
    "GBPUSD=X":  "GBP / USD",
    "JPYUSD=X":  "JPY / USD",
    "EURUSD=X":  "EUR / USD",
    "AUDUSD=X":  "AUD / USD",
}

# ── Master symbol registry (NSE + Crypto + MCX + Forex) ──────────────────────
ALL_SYMBOLS: dict[str, str] = {
    **NSE_STOCKS,
    **CRYPTO_SYMBOLS,
    **MCX_SYMBOLS,
    **FOREX_SYMBOLS,
}

# Universe presets
_NIFTY50_SYMS = [
    "ADANIENT.NS",
    "ADANIPORTS.NS",
    "APOLLOHOSP.NS",
    "ASIANPAINT.NS",
    "AXISBANK.NS",
    "BAJAJ-AUTO.NS",
    "BAJFINANCE.NS",
    "BAJAJFINSV.NS",
    "BEL.NS",
    "BHARTIARTL.NS",
    "CIPLA.NS",
    "COALINDIA.NS",
    "DRREDDY.NS",
    "EICHERMOT.NS",
    "ETERNAL.NS",
    "GRASIM.NS",
    "HCLTECH.NS",
    "HDFCBANK.NS",
    "HDFCLIFE.NS",
    "HINDALCO.NS",
    "HINDUNILVR.NS",
    "ICICIBANK.NS",
    "ITC.NS",
    "INFY.NS",
    "INDIGO.NS",
    "JSWSTEEL.NS",
    "JIOFIN.NS",
    "KOTAKBANK.NS",
    "LT.NS",
    "M&M.NS",
    "MARUTI.NS",
    "MAXHEALTH.NS",
    "NTPC.NS",
    "NESTLEIND.NS",
    "ONGC.NS",
    "POWERGRID.NS",
    "RELIANCE.NS",
    "SBILIFE.NS",
    "SHRIRAMFIN.NS",
    "SBIN.NS",
    "SUNPHARMA.NS",
    "TCS.NS",
    "TATACONSUM.NS",
    "TMPV.NS",
    "TATASTEEL.NS",
    "TECHM.NS",
    "TITAN.NS",
    "TRENT.NS",
    "ULTRACEMCO.NS",
    "WIPRO.NS",
]

_NIFTY100_SYMS = [
    "ADANIENT.NS",
    "ADANIPORTS.NS",
    "APOLLOHOSP.NS",
    "ASIANPAINT.NS",
    "AXISBANK.NS",
    "BAJAJ-AUTO.NS",
    "BAJFINANCE.NS",
    "BAJAJFINSV.NS",
    "BEL.NS",
    "BHARTIARTL.NS",
    "CIPLA.NS",
    "COALINDIA.NS",
    "DRREDDY.NS",
    "EICHERMOT.NS",
    "ETERNAL.NS",
    "GRASIM.NS",
    "HCLTECH.NS",
    "HDFCBANK.NS",
    "HDFCLIFE.NS",
    "HINDALCO.NS",
    "HINDUNILVR.NS",
    "ICICIBANK.NS",
    "ITC.NS",
    "INFY.NS",
    "INDIGO.NS",
    "JSWSTEEL.NS",
    "JIOFIN.NS",
    "KOTAKBANK.NS",
    "LT.NS",
    "M&M.NS",
    "MARUTI.NS",
    "MAXHEALTH.NS",
    "NTPC.NS",
    "NESTLEIND.NS",
    "ONGC.NS",
    "POWERGRID.NS",
    "RELIANCE.NS",
    "SBILIFE.NS",
    "SHRIRAMFIN.NS",
    "SBIN.NS",
    "SUNPHARMA.NS",
    "TCS.NS",
    "TATACONSUM.NS",
    "TMPV.NS",
    "TATASTEEL.NS",
    "TECHM.NS",
    "TITAN.NS",
    "TRENT.NS",
    "ULTRACEMCO.NS",
    "WIPRO.NS",
    "ABB.NS",
    "ADANIENSOL.NS",
    "ADANIGREEN.NS",
    "ADANIPOWER.NS",
    "AMBUJACEM.NS",
    "DMART.NS",
    "BAJAJHLDNG.NS",
    "BANKBARODA.NS",
    "BPCL.NS",
    "BOSCHLTD.NS",
    "BRITANNIA.NS",
    "CGPOWER.NS",
    "CANBK.NS",
    "CHOLAFIN.NS",
    "CUMMINSIND.NS",
    "DLF.NS",
    "DIVISLAB.NS",
    "GAIL.NS",
    "GODREJCP.NS",
    "HDFCAMC.NS",
    "HAL.NS",
    "HINDZINC.NS",
    "HYUNDAI.NS",
    "INDHOTEL.NS",
    "IOC.NS",
    "IRFC.NS",
    "JINDALSTEL.NS",
    "LTM.NS",
    "LODHA.NS",
    "MAZDOCK.NS",
    "MUTHOOTFIN.NS",
    "PIDILITIND.NS",
    "PFC.NS",
    "PNB.NS",
    "RECLTD.NS",
    "MOTHERSON.NS",
    "SHREECEM.NS",
    "ENRIN.NS",
    "SIEMENS.NS",
    "SOLARINDS.NS",
    "TVSMOTOR.NS",
    "TATACAP.NS",
    "TMCV.NS",
    "TATAPOWER.NS",
    "TORNTPHARM.NS",
    "UNIONBANK.NS",
    "UNITDSPR.NS",
    "VBL.NS",
    "VEDL.NS",
    "ZYDUSLIFE.NS",
]

_NIFTY200_SYMS = [
    "360ONE.NS",
    "ABB.NS",
    "APLAPOLLO.NS",
    "AUBANK.NS",
    "ADANIENSOL.NS",
    "ADANIENT.NS",
    "ADANIGREEN.NS",
    "ADANIPORTS.NS",
    "ADANIPOWER.NS",
    "ATGL.NS",
    "ABCAPITAL.NS",
    "ALKEM.NS",
    "AMBUJACEM.NS",
    "APOLLOHOSP.NS",
    "ASHOKLEY.NS",
    "ASIANPAINT.NS",
    "ASTRAL.NS",
    "AUROPHARMA.NS",
    "DMART.NS",
    "AXISBANK.NS",
    "BSE.NS",
    "BAJAJ-AUTO.NS",
    "BAJFINANCE.NS",
    "BAJAJFINSV.NS",
    "BAJAJHLDNG.NS",
    "BANKBARODA.NS",
    "BANKINDIA.NS",
    "BDL.NS",
    "BEL.NS",
    "BHARATFORG.NS",
    "BHEL.NS",
    "BPCL.NS",
    "BHARTIARTL.NS",
    "GROWW.NS",
    "BIOCON.NS",
    "BLUESTARCO.NS",
    "BOSCHLTD.NS",
    "BRITANNIA.NS",
    "CGPOWER.NS",
    "CANBK.NS",
    "CHOLAFIN.NS",
    "CIPLA.NS",
    "COALINDIA.NS",
    "COCHINSHIP.NS",
    "COFORGE.NS",
    "COLPAL.NS",
    "CONCOR.NS",
    "COROMANDEL.NS",
    "CUMMINSIND.NS",
    "DLF.NS",
    "DABUR.NS",
    "DIVISLAB.NS",
    "DIXON.NS",
    "DRREDDY.NS",
    "EICHERMOT.NS",
    "ETERNAL.NS",
    "EXIDEIND.NS",
    "NYKAA.NS",
    "FEDERALBNK.NS",
    "FORTIS.NS",
    "GAIL.NS",
    "GVT&D.NS",
    "GMRAIRPORT.NS",
    "GLENMARK.NS",
    "GODFRYPHLP.NS",
    "GODREJCP.NS",
    "GODREJPROP.NS",
    "GRASIM.NS",
    "HCLTECH.NS",
    "HDFCAMC.NS",
    "HDFCBANK.NS",
    "HDFCLIFE.NS",
    "HAVELLS.NS",
    "HEROMOTOCO.NS",
    "HINDALCO.NS",
    "HAL.NS",
    "HINDPETRO.NS",
    "HINDUNILVR.NS",
    "HINDZINC.NS",
    "POWERINDIA.NS",
    "HUDCO.NS",
    "HYUNDAI.NS",
    "ICICIBANK.NS",
    "ICICIGI.NS",
    "ICICIAMC.NS",
    "IDFCFIRSTB.NS",
    "ITC.NS",
    "INDIANB.NS",
    "INDHOTEL.NS",
    "IOC.NS",
    "IRCTC.NS",
    "IRFC.NS",
    "IREDA.NS",
    "INDUSTOWER.NS",
    "INDUSINDBK.NS",
    "NAUKRI.NS",
    "INFY.NS",
    "INDIGO.NS",
    "JSWENERGY.NS",
    "JSWSTEEL.NS",
    "JINDALSTEL.NS",
    "JIOFIN.NS",
    "JUBLFOOD.NS",
    "KEI.NS",
    "KPITTECH.NS",
    "KALYANKJIL.NS",
    "KOTAKBANK.NS",
    "LTF.NS",
    "LGEINDIA.NS",
    "LICHSGFIN.NS",
    "LTM.NS",
    "LT.NS",
    "LAURUSLABS.NS",
    "LENSKART.NS",
    "LODHA.NS",
    "LUPIN.NS",
    "MRF.NS",
    "M&MFIN.NS",
    "M&M.NS",
    "MANKIND.NS",
    "MARICO.NS",
    "MARUTI.NS",
    "MFSL.NS",
    "MAXHEALTH.NS",
    "MAZDOCK.NS",
    "MOTILALOFS.NS",
    "MPHASIS.NS",
    "MCX.NS",
    "MUTHOOTFIN.NS",
    "NHPC.NS",
    "NMDC.NS",
    "NTPC.NS",
    "NATIONALUM.NS",
    "NESTLEIND.NS",
    "OBEROIRLTY.NS",
    "ONGC.NS",
    "OIL.NS",
    "PAYTM.NS",
    "OFSS.NS",
    "POLICYBZR.NS",
    "PIIND.NS",
    "PAGEIND.NS",
    "PATANJALI.NS",
    "PERSISTENT.NS",
    "PHOENIXLTD.NS",
    "PIDILITIND.NS",
    "POLYCAB.NS",
    "PFC.NS",
    "POWERGRID.NS",
    "PREMIERENE.NS",
    "PRESTIGE.NS",
    "PNB.NS",
    "RECLTD.NS",
    "RADICO.NS",
    "RVNL.NS",
    "RELIANCE.NS",
    "SBICARD.NS",
    "SBILIFE.NS",
    "SRF.NS",
    "MOTHERSON.NS",
    "SHREECEM.NS",
    "SHRIRAMFIN.NS",
    "ENRIN.NS",
    "SIEMENS.NS",
    "SOLARINDS.NS",
    "SBIN.NS",
    "SAIL.NS",
    "SUNPHARMA.NS",
    "SUPREMEIND.NS",
    "SUZLON.NS",
    "SWIGGY.NS",
    "TVSMOTOR.NS",
    "TATACAP.NS",
    "TATACOMM.NS",
    "TCS.NS",
    "TATACONSUM.NS",
    "TATAELXSI.NS",
    "TATAINVEST.NS",
    "TMCV.NS",
    "TMPV.NS",
    "TATAPOWER.NS",
    "TATASTEEL.NS",
    "TECHM.NS",
    "TITAN.NS",
    "TORNTPHARM.NS",
    "TRENT.NS",
    "TIINDIA.NS",
    "UPL.NS",
    "ULTRACEMCO.NS",
    "UNIONBANK.NS",
    "UNITDSPR.NS",
    "VBL.NS",
    "VEDL.NS",
    "VMM.NS",
    "IDEA.NS",
    "VOLTAS.NS",
    "WAAREEENER.NS",
    "WIPRO.NS",
    "YESBANK.NS",
    "ZYDUSLIFE.NS",
]

# Universe presets
UNIVERSES: dict[str, list[str]] = {
    "nifty50":  [s for s in _NIFTY50_SYMS  if s in NSE_STOCKS],
    "nifty100": [s for s in _NIFTY100_SYMS if s in NSE_STOCKS],
    "nifty200": [s for s in _NIFTY200_SYMS if s in NSE_STOCKS],
    "fno":      [s for s in _FNO_SYMBOLS   if s in NSE_STOCKS],   # F&O eligible stocks
    "nifty500": list(NSE_STOCKS.keys()),   # full NSE list
    "crypto":   list(CRYPTO_SYMBOLS.keys()),
    "mcx":      list(MCX_SYMBOLS.keys()),
    "forex":    list(FOREX_SYMBOLS.keys()),
}

# ── Cache ─────────────────────────────────────────────────────────────────────

CACHE_TTL  = 6 * 3600   # 6 hours — ATH doesn't change hourly
MAX_WORKERS = 12

_cache: dict[str, dict] = {}   # universe → {ts, data}
_jobs:  dict[str, dict] = {}   # job_id  → {status, progress, total, results, error, universe}
_lock = threading.Lock()


# ── Core scan logic ───────────────────────────────────────────────────────────

def _check_stock(symbol: str) -> dict | None:
    """
    Fetch up to 10 years of daily OHLCV and derive ATH metrics.

    Returns the stock dict on success. On failure, returns a small
    {"_failed": True, "symbol": ..., "reason": ...} marker instead of a bare
    None — a scan over a large universe used to silently drop failed symbols
    with zero visibility into which ones failed or why (rate limiting,
    delisted ticker, bad symbol, etc). Callers should check for "_failed"
    rather than assuming a non-None return is always a valid stock record.
    """
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period="max", interval="1d", auto_adjust=True)

        if df is None or len(df) < 60:
            got = 0 if df is None else len(df)
            return {"_failed": True, "symbol": symbol,
                    "reason": f"insufficient history ({got} bars, need 60+)"}

        df = df.dropna(subset=["High", "Close"])
        if df.empty:
            return {"_failed": True, "symbol": symbol,
                    "reason": "no valid OHLC data after cleaning"}

        # ── All-time high ────────────────────────────────────────────────────
        ath_idx    = df["High"].idxmax()
        ath_price  = float(df["High"].max())
        ath_date   = ath_idx.date()
        today      = datetime.now(timezone.utc).date()
        days_since = (today - ath_date).days
        ath_in_90d = days_since <= 90

        # ── Current snapshot ─────────────────────────────────────────────────
        current_price = float(df["Close"].iloc[-1])
        pct_from_ath  = round((current_price - ath_price) / ath_price * 100, 2)

        # ── 50 % ATH level ───────────────────────────────────────────────────
        half_ath       = round(ath_price * 0.50, 2)
        dist_50pct     = round((current_price - half_ath) / half_ath * 100, 2)
        near_50pct     = abs(dist_50pct) <= 5          # within ±5 % of the level
        above_50pct    = current_price >= half_ath     # recovered past the mid-line

        # ── Momentum stats ───────────────────────────────────────────────────
        w52_high = round(float(df["High"].tail(252).max()), 2)
        volume   = int(df["Volume"].iloc[-1]) if "Volume" in df.columns else 0

        # ── Volume confirmation ──────────────────────────────────────────────
        # Compares latest volume to the trailing 20-session average so a
        # level-touch on genuinely elevated volume can be told apart from one
        # drifting past on thin, unconfirmed volume.
        if "Volume" in df.columns and len(df) >= 20:
            avg_volume_20 = int(df["Volume"].tail(20).mean())
        else:
            avg_volume_20 = volume
        volume_ratio = round(volume / avg_volume_20, 2) if avg_volume_20 > 0 else 0.0

        # ── Previous close for % change ──────────────────────────────────────
        prev_close = float(df["Close"].iloc[-2]) if len(df) >= 2 else current_price
        day_change_pct = round((current_price - prev_close) / prev_close * 100, 2)

        return {
            "symbol":         symbol,
            "name":           NSE_STOCKS.get(symbol, symbol.replace(".NS", "")),
            "current_price":  round(current_price, 2),
            "prev_close":     round(prev_close, 2),
            "day_change_pct": day_change_pct,
            "all_time_high":  round(ath_price, 2),
            "ath_date":       str(ath_date),
            "days_since_ath": days_since,
            "ath_in_90d":     ath_in_90d,
            "pct_from_ath":   pct_from_ath,
            "half_ath_price": half_ath,
            "near_50pct_ath": near_50pct,
            "above_50pct_ath":above_50pct,
            "dist_from_50pct":dist_50pct,
            "w52_high":       w52_high,
            "volume":         volume,
            "avg_volume_20":  avg_volume_20,
            "volume_ratio":   volume_ratio,
        }
    except Exception as exc:
        return {"_failed": True, "symbol": symbol,
                "reason": f"{type(exc).__name__}: {str(exc)[:150]}"}


def _run_scan_thread(job_id: str, symbols: list[str]) -> None:
    """Background worker — updates _jobs[job_id] in place."""
    with _lock:
        _jobs[job_id]["status"] = "running"

    results: list[dict] = []
    failed:  list[dict] = []
    total = len(symbols)
    done  = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_check_stock, s): s for s in symbols}
        for fut in as_completed(futures):
            done += 1
            result = fut.result()
            if result and not result.get("_failed"):
                results.append(result)
            else:
                sym = (result or {}).get("symbol") or futures[fut]
                failed.append({
                    "symbol": sym,
                    "name":   NSE_STOCKS.get(sym, sym.replace(".NS", "")),
                    "reason": (result or {}).get("reason", "unknown error"),
                })
            with _lock:
                _jobs[job_id]["progress"] = done
                _jobs[job_id]["total"]    = total

    # Sort: ATH stocks first (by days_since_ath asc), then by % from ATH desc
    ath_stocks = sorted(
        [r for r in results if r["ath_in_90d"]],
        key=lambda r: r["days_since_ath"]
    )
    alert_stocks = sorted(
        [r for r in results if r["near_50pct_ath"]],
        key=lambda r: abs(r["dist_from_50pct"])
    )
    all_stocks = sorted(results, key=lambda r: r["pct_from_ath"], reverse=True)

    payload = {
        "ath_90d":      ath_stocks,
        "alert_50pct":  alert_stocks,
        "all":          all_stocks,
        # Visibility into the gap between universe size and results returned —
        # previously symbols just silently vanished with no way to tell why.
        "universe_size": total,
        "returned_count": len(results),
        "failed":         failed,
        "failed_count":   len(failed),
        "scanned_at": datetime.now(timezone.utc).isoformat(),
    }

    # Cache and mark done
    universe = _jobs[job_id].get("universe", "nifty50")
    with _lock:
        _cache[universe] = {"ts": time.time(), "data": payload}
        _jobs[job_id]["status"]  = "done"
        _jobs[job_id]["results"] = payload


# ── Public API ────────────────────────────────────────────────────────────────

def start_scan(universe: str = "nifty50", force: bool = False) -> str:
    """
    Start a background scan (or return immediately from cache).
    Returns a job_id string.

    force: when True, bypasses the 6-hour cache and always launches a fresh
    scan. Without this, re-running the same universe within CACHE_TTL just
    hands back the same cached payload — which, with no freshness indicator
    in the UI, looks indistinguishable from "the scan is stuck."
    """
    universe = universe if universe in UNIVERSES else "nifty50"

    # Return cached results if still fresh (unless the caller asked to force)
    with _lock:
        cached = _cache.get(universe)
        if not force and cached and (time.time() - cached["ts"]) < CACHE_TTL:
            job_id = str(uuid.uuid4())
            _jobs[job_id] = {
                "status":     "done",
                "progress":   len(UNIVERSES[universe]),
                "total":      len(UNIVERSES[universe]),
                "results":    cached["data"],
                "universe":   universe,
                "from_cache": True,
                "cache_age_sec": int(time.time() - cached["ts"]),
            }
            return job_id

    # Launch new scan
    job_id = str(uuid.uuid4())
    symbols = UNIVERSES[universe]
    _jobs[job_id] = {
        "status":     "pending",
        "progress":   0,
        "total":      len(symbols),
        "results":    None,
        "universe":   universe,
        "from_cache": False,
    }
    t = threading.Thread(target=_run_scan_thread, args=(job_id, symbols), daemon=True)
    t.start()
    return job_id


def get_status(job_id: str) -> dict:
    """Poll for scan status. Returns results when status == 'done'."""
    with _lock:
        job = _jobs.get(job_id)
    if not job:
        return {"status": "not_found"}
    return {
        "status":        job["status"],
        "progress":      job["progress"],
        "total":         job["total"],
        "results":       job["results"],
        "from_cache":    job.get("from_cache", False),
        "cache_age_sec": job.get("cache_age_sec"),
    }


def list_universes() -> dict:
    """Return universe names and stock counts."""
    return {name: len(syms) for name, syms in UNIVERSES.items()}


def get_cached(universe: str) -> dict | None:
    """Return cached results for a universe, or None if stale/missing."""
    with _lock:
        c = _cache.get(universe)
        if c and (time.time() - c["ts"]) < CACHE_TTL:
            return c["data"]
    return None


def get_ath_symbols() -> dict:
    """
    Return symbol name-maps for the two ATH filters pulled from ALL cached
    ATH scans (merged across whatever universe was last scanned).

    Returns::

        {
            "ath_90d":  {symbol: name, …},   # stocks that hit ATH in last 90 days
            "near_ath": {symbol: name, …},   # stocks near 50 % of their ATH
        }

    Both dicts are empty when no ATH scan has been run yet.
    """
    ath_90d:  dict[str, str] = {}
    near_ath: dict[str, str] = {}
    with _lock:
        for cached in _cache.values():
            data = cached.get("data", {})
            for r in data.get("ath_90d", []):
                ath_90d[r["symbol"]] = r["name"]
            for r in data.get("alert_50pct", []):
                near_ath[r["symbol"]] = r["name"]
    return {"ath_90d": ath_90d, "near_ath": near_ath}

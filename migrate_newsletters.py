#!/usr/bin/env python3
"""
Migrate newsletter rules from flat category structure to Marketing/Topics two-tier structure.
Updates both the 'name' field (» separated) and move action 'target' (/ separated).

Usage:
    python migrate_newsletters.py --dry-run   # preview changes
    python migrate_newsletters.py             # apply changes
"""

import argparse
import json
import sys
from pathlib import Path

RULES_DIR = Path(__file__).parent / "rules"

SEP = " \u00bb "  # " » "

# Ordered list of (name_prefix, new_name_prefix, target_prefix, new_target_prefix)
# Longest/most-specific matches must come first.
MAPPINGS = [
    # Specific Learning → Product Management
    ("Newsletters » Learning » Mind the Product",   "Newsletters » Topics » Product Management » Mind the Product",   "Newsletters/Learning/Mind the Product",   "Newsletters/Topics/Product Management/Mind the Product"),
    ("Newsletters » Learning » A&J Smart",          "Newsletters » Topics » Product Management » A&J Smart",          "Newsletters/Learning/A&J Smart",          "Newsletters/Topics/Product Management/A&J Smart"),
    ("Newsletters » Learning » Product School",     "Newsletters » Topics » Product Management » Product School",     "Newsletters/Learning/Product School",     "Newsletters/Topics/Product Management/Product School"),
    ("Newsletters » Learning » Agile Rabbit",       "Newsletters » Topics » Product Management » Agile Rabbit",       "Newsletters/Learning/Agile Rabbit",       "Newsletters/Topics/Product Management/Agile Rabbit"),
    ("Newsletters » Learning » Allan Kelly",        "Newsletters » Topics » Product Management » Allan Kelly",        "Newsletters/Learning/Allan Kelly",        "Newsletters/Topics/Product Management/Allan Kelly"),

    # Specific Travel → Topics/Media
    ("Newsletters » Travel » The Points Guy",       "Newsletters » Topics » Media » The Points Guy",                  "Newsletters/Travel/The Points Guy",       "Newsletters/Topics/Media/The Points Guy"),
    ("Newsletters » Travel » Jacks Flight Club",    "Newsletters » Topics » Media » Jacks Flight Club",               "Newsletters/Travel/Jacks Flight Club",    "Newsletters/Topics/Media/Jacks Flight Club"),

    # Specific Finance → Topics/Finance & Economics
    ("Newsletters » Finance » MSE Money Tips",      "Newsletters » Topics » Finance & Economics » MSE Money Tips",    "Newsletters/Finance/MSE Money Tips",      "Newsletters/Topics/Finance & Economics/MSE Money Tips"),

    # General category mappings — Marketing
    ("Newsletters » Art",             "Newsletters » Marketing » Art",             "Newsletters/Art",             "Newsletters/Marketing/Art"),
    ("Newsletters » Coffee",          "Newsletters » Marketing » Coffee",          "Newsletters/Coffee",          "Newsletters/Marketing/Coffee"),
    ("Newsletters » Disney Holidays", "Newsletters » Marketing » Disney Holidays", "Newsletters/Disney Holidays", "Newsletters/Marketing/Disney Holidays"),
    ("Newsletters » Entertainment",   "Newsletters » Marketing » Entertainment",   "Newsletters/Entertainment",   "Newsletters/Marketing/Entertainment"),
    ("Newsletters » Finance",         "Newsletters » Marketing » Finance",         "Newsletters/Finance",         "Newsletters/Marketing/Finance"),
    ("Newsletters » Fitness",         "Newsletters » Marketing » Fitness",         "Newsletters/Fitness",         "Newsletters/Marketing/Fitness"),
    ("Newsletters » Food",            "Newsletters » Marketing » Food",            "Newsletters/Food",            "Newsletters/Marketing/Food"),
    ("Newsletters » Garden",          "Newsletters » Marketing » Garden",          "Newsletters/Garden",          "Newsletters/Marketing/Garden"),
    ("Newsletters » Health",          "Newsletters » Marketing » Health",          "Newsletters/Health",          "Newsletters/Marketing/Health"),
    ("Newsletters » Hotels",          "Newsletters » Marketing » Hotels",          "Newsletters/Hotels",          "Newsletters/Marketing/Hotels"),
    ("Newsletters » Outdoors",        "Newsletters » Marketing » Outdoors",        "Newsletters/Outdoors",        "Newsletters/Marketing/Outdoors"),
    ("Newsletters » Photo",           "Newsletters » Marketing » Photo",           "Newsletters/Photo",           "Newsletters/Marketing/Photo"),
    ("Newsletters » Property",        "Newsletters » Marketing » Property",        "Newsletters/Property",        "Newsletters/Marketing/Property"),
    ("Newsletters » Restaurants",     "Newsletters » Marketing » Restaurants",     "Newsletters/Restaurants",     "Newsletters/Marketing/Restaurants"),
    ("Newsletters » Retail",          "Newsletters » Marketing » Retail",          "Newsletters/Retail",          "Newsletters/Marketing/Retail"),
    ("Newsletters » Student Discount","Newsletters » Marketing » Student Discount","Newsletters/Student Discount","Newsletters/Marketing/Student Discount"),
    ("Newsletters » Tech",            "Newsletters » Marketing » Tech",            "Newsletters/Tech",            "Newsletters/Marketing/Tech"),
    ("Newsletters » Tools",           "Newsletters » Marketing » Tools",           "Newsletters/Tools",           "Newsletters/Marketing/Tools"),
    ("Newsletters » Travel",          "Newsletters » Marketing » Travel",          "Newsletters/Travel",          "Newsletters/Marketing/Travel"),
    ("Newsletters » Virgin",          "Newsletters » Marketing » Virgin",          "Newsletters/Virgin",          "Newsletters/Marketing/Virgin"),
    ("Newsletters » Vouchers",        "Newsletters » Marketing » Vouchers",        "Newsletters/Vouchers",        "Newsletters/Marketing/Vouchers"),
    ("Newsletters » Wine",            "Newsletters » Marketing » Wine",            "Newsletters/Wine",            "Newsletters/Marketing/Wine"),

    # General category mappings — Topics
    ("Newsletters » Charity",         "Newsletters » Topics » Charity",            "Newsletters/Charity",         "Newsletters/Topics/Charity"),
    ("Newsletters » Learning",        "Newsletters » Topics » Learning",           "Newsletters/Learning",        "Newsletters/Topics/Learning"),
    ("Newsletters » Media",           "Newsletters » Topics » Media",              "Newsletters/Media",           "Newsletters/Topics/Media"),

    # Partial-group fixes — more specific prefixes, must come before their parent entries below
    ("Newsletters » Kickstarter » Tevaplanter", "Newsletters » Marketing » Retail » Tevaplanter",            "Newsletters/Kickstarter/Tevaplanter", "Newsletters/Marketing/Retail/Tevaplanter"),
    ("Newsletters » Events » CES",              "Newsletters » Marketing » Tech » CES",                      "Newsletters/Events/CES",              "Newsletters/Marketing/Tech/CES"),
    ("Newsletters » Music » Metric",            "Newsletters » Marketing » Entertainment » Metric",           "Newsletters/Music/Metric",            "Newsletters/Marketing/Entertainment/Metric"),
    ("Newsletters » Personal Finance » Loqbox", "Newsletters » Topics » Finance & Economics » Loqbox",        "Newsletters/Personal Finance/Loqbox", "Newsletters/Topics/Finance & Economics/Loqbox"),
    ("Newsletters » Services » TouchNote",      "Newsletters » Marketing » Retail » TouchNote",               "Newsletters/Services/TouchNote",      "Newsletters/Marketing/Retail/TouchNote"),
    ("Newsletters » Topic » Exe Estuary",       "Newsletters » Topics » Media » Exe Estuary",                 "Newsletters/Topic/Exe Estuary",       "Newsletters/Topics/Media/Exe Estuary"),

    # Ungrouped → Topics/Media
    ("Newsletters » BBC",              "Newsletters » Topics » Media » BBC",              "Newsletters/BBC",              "Newsletters/Topics/Media/BBC"),
    ("Newsletters » Democracy Club",   "Newsletters » Topics » Media » Democracy Club",   "Newsletters/Democracy Club",   "Newsletters/Topics/Media/Democracy Club"),
    ("Newsletters » Economist",        "Newsletters » Topics » Media » Economist",        "Newsletters/Economist",        "Newsletters/Topics/Media/Economist"),
    ("Newsletters » New York Times",   "Newsletters » Topics » Media » New York Times",   "Newsletters/New York Times",   "Newsletters/Topics/Media/New York Times"),
    ("Newsletters » Royal Family",     "Newsletters » Topics » Media » Royal Family",     "Newsletters/Royal Family",     "Newsletters/Topics/Media/Royal Family"),
    ("Newsletters » The Guardian",     "Newsletters » Topics » Media » The Guardian",     "Newsletters/The Guardian",     "Newsletters/Topics/Media/The Guardian"),
    ("Newsletters » Time and Date",    "Newsletters » Topics » Media » Time and Date",    "Newsletters/Time and Date",    "Newsletters/Topics/Media/Time and Date"),
    ("Newsletters » Which?",           "Newsletters » Topics » Media » Which?",           "Newsletters/Which?",           "Newsletters/Topics/Media/Which?"),
    ("Newsletters » Wikipedia",        "Newsletters » Topics » Media » Wikipedia",        "Newsletters/Wikipedia",        "Newsletters/Topics/Media/Wikipedia"),

    # Ungrouped → Topics/Learning
    ("Newsletters » Coursera",                "Newsletters » Topics » Learning » Coursera",                "Newsletters/Coursera",                "Newsletters/Topics/Learning/Coursera"),
    ("Newsletters » Create and Cultivate",    "Newsletters » Topics » Learning » Create and Cultivate",    "Newsletters/Create and Cultivate",    "Newsletters/Topics/Learning/Create and Cultivate"),
    ("Newsletters » Duolingo",                "Newsletters » Topics » Learning » Duolingo",                "Newsletters/Duolingo",                "Newsletters/Topics/Learning/Duolingo"),
    ("Newsletters » Goal Plans",              "Newsletters » Topics » Learning » Goal Plans",              "Newsletters/Goal Plans",              "Newsletters/Topics/Learning/Goal Plans"),
    ("Newsletters » Productivity Flourising", "Newsletters » Topics » Learning » Productivity Flourising", "Newsletters/Productivity Flourising", "Newsletters/Topics/Learning/Productivity Flourising"),
    ("Newsletters » Skillshare",              "Newsletters » Topics » Learning » Skillshare",              "Newsletters/Skillshare",              "Newsletters/Topics/Learning/Skillshare"),
    ("Newsletters » Udemy",                   "Newsletters » Topics » Learning » Udemy",                   "Newsletters/Udemy",                   "Newsletters/Topics/Learning/Udemy"),

    # Ungrouped → Topics/Product Management
    ("Newsletters » Product School", "Newsletters » Topics » Product Management » Product School", "Newsletters/Product School", "Newsletters/Topics/Product Management/Product School"),

    # Ungrouped → Topics/Charity
    ("Newsletters » Cancer Research UK", "Newsletters » Topics » Charity » Cancer Research UK", "Newsletters/Cancer Research UK", "Newsletters/Topics/Charity/Cancer Research UK"),
    ("Newsletters » Hope For Children",  "Newsletters » Topics » Charity » Hope For Children",  "Newsletters/Hope For Children",  "Newsletters/Topics/Charity/Hope For Children"),

    # Ungrouped → Marketing/Tech
    ("Newsletters » Amazon Web Services", "Newsletters » Marketing » Tech » Amazon Web Services", "Newsletters/Amazon Web Services", "Newsletters/Marketing/Tech/Amazon Web Services"),
    ("Newsletters » Creatable",           "Newsletters » Marketing » Tech » Creatable",           "Newsletters/Creatable",           "Newsletters/Marketing/Tech/Creatable"),
    ("Newsletters » dbrand",              "Newsletters » Marketing » Tech » dbrand",              "Newsletters/dbrand",              "Newsletters/Marketing/Tech/dbrand"),
    ("Newsletters » Dyson",               "Newsletters » Marketing » Tech » Dyson",               "Newsletters/Dyson",               "Newsletters/Marketing/Tech/Dyson"),
    ("Newsletters » EasyUsenet",          "Newsletters » Marketing » Tech » EasyUsenet",          "Newsletters/EasyUsenet",          "Newsletters/Marketing/Tech/EasyUsenet"),
    ("Newsletters » Fastmail",            "Newsletters » Marketing » Tech » Fastmail",            "Newsletters/Fastmail",            "Newsletters/Marketing/Tech/Fastmail"),
    ("Newsletters » Good Gadget Deals",   "Newsletters » Marketing » Tech » Good Gadget Deals",   "Newsletters/Good Gadget Deals",   "Newsletters/Marketing/Tech/Good Gadget Deals"),
    ("Newsletters » Homelabbers Hangout", "Newsletters » Marketing » Tech » Homelabbers Hangout", "Newsletters/Homelabbers Hangout", "Newsletters/Marketing/Tech/Homelabbers Hangout"),
    ("Newsletters » Mac DVD Ripper Pro",  "Newsletters » Marketing » Tech » Mac DVD Ripper Pro",  "Newsletters/Mac DVD Ripper Pro",  "Newsletters/Marketing/Tech/Mac DVD Ripper Pro"),
    ("Newsletters » Microsoft",           "Newsletters » Marketing » Tech » Microsoft",           "Newsletters/Microsoft",           "Newsletters/Marketing/Tech/Microsoft"),
    ("Newsletters » NZB Finder",          "Newsletters » Marketing » Tech » NZB Finder",          "Newsletters/NZB Finder",          "Newsletters/Marketing/Tech/NZB Finder"),
    ("Newsletters » NZBgeek",             "Newsletters » Marketing » Tech » NZBgeek",             "Newsletters/NZBgeek",             "Newsletters/Marketing/Tech/NZBgeek"),
    ("Newsletters » OpenAI",              "Newsletters » Marketing » Tech » OpenAI",              "Newsletters/OpenAI",              "Newsletters/Marketing/Tech/OpenAI"),
    ("Newsletters » Philips",             "Newsletters » Marketing » Tech » Philips",             "Newsletters/Philips",             "Newsletters/Marketing/Tech/Philips"),
    ("Newsletters » Raylo",               "Newsletters » Marketing » Tech » Raylo",               "Newsletters/Raylo",               "Newsletters/Marketing/Tech/Raylo"),
    ("Newsletters » RS Components",       "Newsletters » Marketing » Tech » RS Components",       "Newsletters/RS Components",       "Newsletters/Marketing/Tech/RS Components"),
    ("Newsletters » WeBuyAnyPhone",       "Newsletters » Marketing » Tech » WeBuyAnyPhone",       "Newsletters/WeBuyAnyPhone",       "Newsletters/Marketing/Tech/WeBuyAnyPhone"),

    # Ungrouped → Marketing/Travel
    ("Newsletters » Airalo",               "Newsletters » Marketing » Travel » Airalo",               "Newsletters/Airalo",               "Newsletters/Marketing/Travel/Airalo"),
    ("Newsletters » Amtrak",               "Newsletters » Marketing » Travel » Amtrak",               "Newsletters/Amtrak",               "Newsletters/Marketing/Travel/Amtrak"),
    ("Newsletters » Bristol Airport",      "Newsletters » Marketing » Travel » Bristol Airport",      "Newsletters/Bristol Airport",      "Newsletters/Marketing/Travel/Bristol Airport"),
    ("Newsletters » CrossCountry",         "Newsletters » Marketing » Travel » CrossCountry",         "Newsletters/CrossCountry",         "Newsletters/Marketing/Travel/CrossCountry"),
    ("Newsletters » Grand Central Terminal","Newsletters » Marketing » Travel » Grand Central Terminal","Newsletters/Grand Central Terminal","Newsletters/Marketing/Travel/Grand Central Terminal"),
    ("Newsletters » Heathrow Express",     "Newsletters » Marketing » Travel » Heathrow Express",     "Newsletters/Heathrow Express",     "Newsletters/Marketing/Travel/Heathrow Express"),
    ("Newsletters » Hopper",               "Newsletters » Marketing » Travel » Hopper",               "Newsletters/Hopper",               "Newsletters/Marketing/Travel/Hopper"),
    ("Newsletters » Hostelworld",          "Newsletters » Marketing » Travel » Hostelworld",          "Newsletters/Hostelworld",          "Newsletters/Marketing/Travel/Hostelworld"),
    ("Newsletters » ITS",                  "Newsletters » Marketing » Travel » ITS",                  "Newsletters/ITS",                  "Newsletters/Marketing/Travel/ITS"),
    ("Newsletters » Railcard",             "Newsletters » Marketing » Travel » Railcard",             "Newsletters/Railcard",             "Newsletters/Marketing/Travel/Railcard"),
    ("Newsletters » Sell My Miles",        "Newsletters » Marketing » Travel » Sell My Miles",        "Newsletters/Sell My Miles",        "Newsletters/Marketing/Travel/Sell My Miles"),
    ("Newsletters » Uber",                 "Newsletters » Marketing » Travel » Uber",                 "Newsletters/Uber",                 "Newsletters/Marketing/Travel/Uber"),
    ("Newsletters » Voi",                  "Newsletters » Marketing » Travel » Voi",                  "Newsletters/Voi",                  "Newsletters/Marketing/Travel/Voi"),

    # Ungrouped → Marketing/Hotels
    ("Newsletters » Blue Lagoon",      "Newsletters » Marketing » Hotels » Blue Lagoon",      "Newsletters/Blue Lagoon",      "Newsletters/Marketing/Hotels/Blue Lagoon"),
    ("Newsletters » Catalonia Hotels", "Newsletters » Marketing » Hotels » Catalonia Hotels", "Newsletters/Catalonia Hotels", "Newsletters/Marketing/Hotels/Catalonia Hotels"),
    ("Newsletters » Marriot Bonvoy",   "Newsletters » Marketing » Hotels » Marriot Bonvoy",   "Newsletters/Marriot Bonvoy",   "Newsletters/Marketing/Hotels/Marriot Bonvoy"),
    ("Newsletters » Travelodge",       "Newsletters » Marketing » Hotels » Travelodge",       "Newsletters/Travelodge",       "Newsletters/Marketing/Hotels/Travelodge"),

    # Ungrouped → Marketing/Restaurants
    ("Newsletters » Boatyard Bakery",   "Newsletters » Marketing » Restaurants » Boatyard Bakery",   "Newsletters/Boatyard Bakery",   "Newsletters/Marketing/Restaurants/Boatyard Bakery"),
    ("Newsletters » Boston Tea Party",  "Newsletters » Marketing » Restaurants » Boston Tea Party",  "Newsletters/Boston Tea Party",  "Newsletters/Marketing/Restaurants/Boston Tea Party"),
    ("Newsletters » Chopstix",          "Newsletters » Marketing » Restaurants » Chopstix",          "Newsletters/Chopstix",          "Newsletters/Marketing/Restaurants/Chopstix"),
    ("Newsletters » Dominos",           "Newsletters » Marketing » Restaurants » Dominos",           "Newsletters/Dominos",           "Newsletters/Marketing/Restaurants/Dominos"),
    ("Newsletters » Franco Manca",      "Newsletters » Marketing » Restaurants » Franco Manca",      "Newsletters/Franco Manca",      "Newsletters/Marketing/Restaurants/Franco Manca"),
    ("Newsletters » Gourmet Society",   "Newsletters » Marketing » Restaurants » Gourmet Society",   "Newsletters/Gourmet Society",   "Newsletters/Marketing/Restaurants/Gourmet Society"),
    ("Newsletters » Hall and Woodhouse","Newsletters » Marketing » Restaurants » Hall and Woodhouse","Newsletters/Hall and Woodhouse","Newsletters/Marketing/Restaurants/Hall and Woodhouse"),
    ("Newsletters » The Brunswick",     "Newsletters » Marketing » Restaurants » The Brunswick",     "Newsletters/The Brunswick",     "Newsletters/Marketing/Restaurants/The Brunswick"),

    # Ungrouped → Marketing/Food
    ("Newsletters » Beer52",            "Newsletters » Marketing » Food » Beer52",            "Newsletters/Beer52",            "Newsletters/Marketing/Food/Beer52"),
    ("Newsletters » Eat This Much",     "Newsletters » Marketing » Food » Eat This Much",     "Newsletters/Eat This Much",     "Newsletters/Marketing/Food/Eat This Much"),
    ("Newsletters » Food Talk Daily",   "Newsletters » Marketing » Food » Food Talk Daily",   "Newsletters/Food Talk Daily",   "Newsletters/Marketing/Food/Food Talk Daily"),
    ("Newsletters » Hop Burns and Black","Newsletters » Marketing » Food » Hop Burns and Black","Newsletters/Hop Burns and Black","Newsletters/Marketing/Food/Hop Burns and Black"),
    ("Newsletters » Huel",              "Newsletters » Marketing » Food » Huel",              "Newsletters/Huel",              "Newsletters/Marketing/Food/Huel"),

    # Ungrouped → Marketing/Wine
    ("Newsletters » Naked Wines", "Newsletters » Marketing » Wine » Naked Wines", "Newsletters/Naked Wines", "Newsletters/Marketing/Wine/Naked Wines"),

    # Ungrouped → Marketing/Fitness
    ("Newsletters » Airofit",    "Newsletters » Marketing » Fitness » Airofit",    "Newsletters/Airofit",    "Newsletters/Marketing/Fitness/Airofit"),
    ("Newsletters » box-co-uk",  "Newsletters » Marketing » Fitness » box-co-uk",  "Newsletters/box-co-uk",  "Newsletters/Marketing/Fitness/box-co-uk"),
    ("Newsletters » Buddyboost", "Newsletters » Marketing » Fitness » Buddyboost", "Newsletters/Buddyboost", "Newsletters/Marketing/Fitness/Buddyboost"),
    ("Newsletters » Tribe",      "Newsletters » Marketing » Fitness » Tribe",      "Newsletters/Tribe",      "Newsletters/Marketing/Fitness/Tribe"),

    # Ungrouped → Marketing/Finance
    ("Newsletters » Confused",    "Newsletters » Marketing » Finance » Confused",    "Newsletters/Confused",    "Newsletters/Marketing/Finance/Confused"),
    ("Newsletters » Crypto-com",  "Newsletters » Marketing » Finance » Crypto-com",  "Newsletters/Crypto-com",  "Newsletters/Marketing/Finance/Crypto-com"),
    ("Newsletters » Curve",       "Newsletters » Marketing » Finance » Curve",       "Newsletters/Curve",       "Newsletters/Marketing/Finance/Curve"),
    ("Newsletters » Klarna",      "Newsletters » Marketing » Finance » Klarna",      "Newsletters/Klarna",      "Newsletters/Marketing/Finance/Klarna"),
    ("Newsletters » Nude",        "Newsletters » Marketing » Finance » Nude",        "Newsletters/Nude",        "Newsletters/Marketing/Finance/Nude"),
    ("Newsletters » V12",         "Newsletters » Marketing » Finance » V12",         "Newsletters/V12",         "Newsletters/Marketing/Finance/V12"),

    # Ungrouped → Marketing/Entertainment
    ("Newsletters » 3LAU",            "Newsletters » Marketing » Entertainment » 3LAU",            "Newsletters/3LAU",            "Newsletters/Marketing/Entertainment/3LAU"),
    ("Newsletters » Bumble",          "Newsletters » Marketing » Entertainment » Bumble",          "Newsletters/Bumble",          "Newsletters/Marketing/Entertainment/Bumble"),
    ("Newsletters » Comic Con",       "Newsletters » Marketing » Entertainment » Comic Con",       "Newsletters/Comic Con",       "Newsletters/Marketing/Entertainment/Comic Con"),
    ("Newsletters » Design My Night", "Newsletters » Marketing » Entertainment » Design My Night", "Newsletters/Design My Night", "Newsletters/Marketing/Entertainment/Design My Night"),
    ("Newsletters » Disney+",         "Newsletters » Marketing » Entertainment » Disney+",         "Newsletters/Disney+",         "Newsletters/Marketing/Entertainment/Disney+"),
    ("Newsletters » Dudesnude",       "Newsletters » Marketing » Entertainment » Dudesnude",       "Newsletters/Dudesnude",       "Newsletters/Marketing/Entertainment/Dudesnude"),
    ("Newsletters » Exeter Phoenix",  "Newsletters » Marketing » Entertainment » Exeter Phoenix",  "Newsletters/Exeter Phoenix",  "Newsletters/Marketing/Entertainment/Exeter Phoenix"),
    ("Newsletters » Feeld",           "Newsletters » Marketing » Entertainment » Feeld",           "Newsletters/Feeld",           "Newsletters/Marketing/Entertainment/Feeld"),
    ("Newsletters » IMDB",            "Newsletters » Marketing » Entertainment » IMDB",            "Newsletters/IMDB",            "Newsletters/Marketing/Entertainment/IMDB"),
    ("Newsletters » Rock The Boat",   "Newsletters » Marketing » Entertainment » Rock The Boat",   "Newsletters/Rock The Boat",   "Newsletters/Marketing/Entertainment/Rock The Boat"),

    # Ungrouped → Marketing/Outdoors
    ("Newsletters » Blithfield SC",    "Newsletters » Marketing » Outdoors » Blithfield SC",    "Newsletters/Blithfield SC",    "Newsletters/Marketing/Outdoors/Blithfield SC"),
    ("Newsletters » Dartmoor Classic", "Newsletters » Marketing » Outdoors » Dartmoor Classic", "Newsletters/Dartmoor Classic", "Newsletters/Marketing/Outdoors/Dartmoor Classic"),

    # Ungrouped → Marketing/Art
    ("Newsletters » Castle Fine Art",          "Newsletters » Marketing » Art » Castle Fine Art",          "Newsletters/Castle Fine Art",          "Newsletters/Marketing/Art/Castle Fine Art"),
    ("Newsletters » Lisa Holt Design",         "Newsletters » Marketing » Art » Lisa Holt Design",         "Newsletters/Lisa Holt Design",         "Newsletters/Marketing/Art/Lisa Holt Design"),
    ("Newsletters » National Portrait Gallery","Newsletters » Marketing » Art » National Portrait Gallery","Newsletters/National Portrait Gallery","Newsletters/Marketing/Art/National Portrait Gallery"),
    ("Newsletters » Tate",                     "Newsletters » Marketing » Art » Tate",                     "Newsletters/Tate",                     "Newsletters/Marketing/Art/Tate"),

    # Ungrouped → Marketing/Work
    ("Newsletters » Grafter",  "Newsletters » Marketing » Work » Grafter",  "Newsletters/Grafter",  "Newsletters/Marketing/Work/Grafter"),
    ("Newsletters » LinkedIn", "Newsletters » Marketing » Work » LinkedIn", "Newsletters/LinkedIn", "Newsletters/Marketing/Work/LinkedIn"),

    # Ungrouped → Marketing/Photo
    ("Newsletters » JPEGmini",    "Newsletters » Marketing » Photo » JPEGmini",    "Newsletters/JPEGmini",    "Newsletters/Marketing/Photo/JPEGmini"),
    ("Newsletters » PixDiscount", "Newsletters » Marketing » Photo » PixDiscount", "Newsletters/PixDiscount", "Newsletters/Marketing/Photo/PixDiscount"),

    # Ungrouped → Marketing/Retail (catch-all)
    ("Newsletters » B and Q",           "Newsletters » Marketing » Retail » B and Q",           "Newsletters/B and Q",           "Newsletters/Marketing/Retail/B and Q"),
    ("Newsletters » Brooksdale",        "Newsletters » Marketing » Retail » Brooksdale",        "Newsletters/Brooksdale",        "Newsletters/Marketing/Retail/Brooksdale"),
    ("Newsletters » Drinkstuff",        "Newsletters » Marketing » Retail » Drinkstuff",        "Newsletters/Drinkstuff",        "Newsletters/Marketing/Retail/Drinkstuff"),
    ("Newsletters » Handmade Candle Co","Newsletters » Marketing » Retail » Handmade Candle Co","Newsletters/Handmade Candle Co","Newsletters/Marketing/Retail/Handmade Candle Co"),
    ("Newsletters » Holister",          "Newsletters » Marketing » Retail » Holister",          "Newsletters/Holister",          "Newsletters/Marketing/Retail/Holister"),
    ("Newsletters » Homesense",         "Newsletters » Marketing » Retail » Homesense",         "Newsletters/Homesense",         "Newsletters/Marketing/Retail/Homesense"),
    ("Newsletters » IKEA",              "Newsletters » Marketing » Retail » IKEA",              "Newsletters/IKEA",              "Newsletters/Marketing/Retail/IKEA"),
    ("Newsletters » Ikea Family",       "Newsletters » Marketing » Retail » Ikea Family",       "Newsletters/Ikea Family",       "Newsletters/Marketing/Retail/Ikea Family"),
    ("Newsletters » Karmanow",          "Newsletters » Marketing » Retail » Karmanow",          "Newsletters/Karmanow",          "Newsletters/Marketing/Retail/Karmanow"),
    ("Newsletters » Kickstarter",       "Newsletters » Marketing » Retail » Kickstarter",       "Newsletters/Kickstarter",       "Newsletters/Marketing/Retail/Kickstarter"),
    ("Newsletters » OutSpot",           "Newsletters » Marketing » Retail » OutSpot",           "Newsletters/OutSpot",           "Newsletters/Marketing/Retail/OutSpot"),
    ("Newsletters » Princesshay",       "Newsletters » Marketing » Retail » Princesshay",       "Newsletters/Princesshay",       "Newsletters/Marketing/Retail/Princesshay"),
    ("Newsletters » PuzzleYou",         "Newsletters » Marketing » Retail » PuzzleYou",         "Newsletters/PuzzleYou",         "Newsletters/Marketing/Retail/PuzzleYou"),
    ("Newsletters » South West Water",  "Newsletters » Marketing » Retail » South West Water",  "Newsletters/South West Water",  "Newsletters/Marketing/Retail/South West Water"),
    ("Newsletters » tevaplanter",       "Newsletters » Marketing » Retail » Tevaplanter",       "Newsletters/tevaplanter",       "Newsletters/Marketing/Retail/Tevaplanter"),
    ("Newsletters » UPS",               "Newsletters » Marketing » Retail » UPS",               "Newsletters/UPS",               "Newsletters/Marketing/Retail/UPS"),
    ("Newsletters » Utterly Printable", "Newsletters » Marketing » Retail » Utterly Printable", "Newsletters/Utterly Printable", "Newsletters/Marketing/Retail/Utterly Printable"),
    ("Newsletters » Zebuci",            "Newsletters » Marketing » Retail » Zebuci",            "Newsletters/Zebuci",            "Newsletters/Marketing/Retail/Zebuci"),

    # Missed entries — ClubTan, Caffe Nero (→ Coffee), Peek Home (→ Property)
    ("Newsletters » ClubTan",      "Newsletters » Marketing » Health » ClubTan",          "Newsletters/ClubTan",       "Newsletters/Marketing/Health/ClubTan"),
    ("Newsletters » Caffè Nero",   "Newsletters » Marketing » Coffee » Caffè Nero",        "Newsletters/Caffe Nero",    "Newsletters/Marketing/Coffee/Caffe Nero"),
    ("Newsletters » Peek Home",    "Newsletters » Marketing » Property » Peek Home",       "Newsletters/Peek Home",     "Newsletters/Marketing/Property/Peek Home"),

    # Spelling-variant names (name and target use different strings)
    ("Newsletters » B&Q",          "Newsletters » Marketing » Retail » B and Q",           "Newsletters/B and Q",       "Newsletters/Marketing/Retail/B and Q"),
    ("Newsletters » Crypto.com",   "Newsletters » Marketing » Finance » Crypto.com",       "Newsletters/Crypto-com",    "Newsletters/Marketing/Finance/Crypto-com"),

    # No-prefix rules — name was set without "Newsletters » " prefix
    ("Boatyard Bakery",            "Newsletters » Marketing » Restaurants » Boatyard Bakery","Newsletters/Boatyard Bakery","Newsletters/Marketing/Restaurants/Boatyard Bakery"),
    ("Voi",                        "Newsletters » Marketing » Travel » Voi",               "Newsletters/Voi",           "Newsletters/Marketing/Travel/Voi"),
    ("Disney+",                    "Newsletters » Marketing » Entertainment » Disney+",    "Newsletters/Disney+",       "Newsletters/Marketing/Entertainment/Disney+"),
    ("WeBuyAnyPhone",              "Newsletters » Marketing » Tech » WeBuyAnyPhone",       "Newsletters/WeBuyAnyPhone", "Newsletters/Marketing/Tech/WeBuyAnyPhone"),
    ("Sell My Miles",              "Newsletters » Marketing » Travel » Sell My Miles",     "Newsletters/Sell My Miles", "Newsletters/Marketing/Travel/Sell My Miles"),
    ("Bristol Airport",            "Newsletters » Marketing » Travel » Bristol Airport",   "Newsletters/Bristol Airport","Newsletters/Marketing/Travel/Bristol Airport"),
    ("Castle Fine Art",            "Newsletters » Marketing » Art » Castle Fine Art",      "Newsletters/Castle Fine Art","Newsletters/Marketing/Art/Castle Fine Art"),
    ("Hostelworld",                "Newsletters » Marketing » Travel » Hostelworld",       "Newsletters/Hostelworld",   "Newsletters/Marketing/Travel/Hostelworld"),
    ("V12",                        "Newsletters » Marketing » Finance » V12",              "Newsletters/V12",           "Newsletters/Marketing/Finance/V12"),
    ("Dyson",                      "Newsletters » Marketing » Tech » Dyson",               "Newsletters/Dyson",         "Newsletters/Marketing/Tech/Dyson"),
    ("Marriot Bonvoy",             "Newsletters » Marketing » Hotels » Marriot Bonvoy",    "Newsletters/Marriot Bonvoy","Newsletters/Marketing/Hotels/Marriot Bonvoy"),
    ("Holister",                   "Newsletters » Marketing » Retail » Holister",          "Newsletters/Holister",      "Newsletters/Marketing/Retail/Holister"),
    ("Confused-com",               "Newsletters » Marketing » Finance » Confused",         "Newsletters/Confused",      "Newsletters/Marketing/Finance/Confused"),
    ("Nude",                       "Newsletters » Marketing » Finance » Nude",             "Newsletters/Nude",          "Newsletters/Marketing/Finance/Nude"),
    ("Amtrak",                     "Newsletters » Marketing » Travel » Amtrak",            "Newsletters/Amtrak",        "Newsletters/Marketing/Travel/Amtrak"),
    ("dbrand",                     "Newsletters » Marketing » Tech » dbrand",              "Newsletters/dbrand",        "Newsletters/Marketing/Tech/dbrand"),
    ("Productivity Flourising",    "Newsletters » Topics » Learning » Productivity Flourising","Newsletters/Productivity Flourising","Newsletters/Topics/Learning/Productivity Flourising"),
    ("Product School",             "Newsletters » Topics » Product Management » Product School","Newsletters/Product School","Newsletters/Topics/Product Management/Product School"),
    ("Ikea Family",                "Newsletters » Marketing » Retail » Ikea Family",       "Newsletters/Ikea Family",   "Newsletters/Marketing/Retail/Ikea Family"),
    ("Wikipedia",                  "Newsletters » Topics » Media » Wikipedia",             "Newsletters/Wikipedia",     "Newsletters/Topics/Media/Wikipedia"),
    ("Railcard",                   "Newsletters » Marketing » Travel » Railcard",          "Newsletters/Railcard",      "Newsletters/Marketing/Travel/Railcard"),
    ("Fastmail",                   "Newsletters » Marketing » Tech » Fastmail",            "Newsletters/Fastmail",      "Newsletters/Marketing/Tech/Fastmail"),
    ("Kickstarter",                "Newsletters » Marketing » Retail » Kickstarter",       "Newsletters/Kickstarter",   "Newsletters/Marketing/Retail/Kickstarter"),
    ("Buddyboost",                 "Newsletters » Marketing » Fitness » Buddyboost",       "Newsletters/Buddyboost",    "Newsletters/Marketing/Fitness/Buddyboost"),
]


def find_mapping(name: str):
    for name_prefix, new_name_prefix, target_prefix, new_target_prefix in MAPPINGS:
        if name == name_prefix or name.startswith(name_prefix + " »"):
            return name_prefix, new_name_prefix, target_prefix, new_target_prefix
    return None


def migrate_rule(data: dict, mapping):
    name_prefix, new_name_prefix, target_prefix, new_target_prefix = mapping
    changed = False

    old_name = data.get("name", "")
    if old_name.startswith(name_prefix):
        new_name = new_name_prefix + old_name[len(name_prefix):]
        if new_name != old_name:
            data["name"] = new_name
            changed = True

    for action in data.get("actions", []):
        if action.get("type") == "move":
            old_target = action.get("target", "")
            if old_target.startswith(target_prefix):
                new_target = new_target_prefix + old_target[len(target_prefix):]
                if new_target != old_target:
                    action["target"] = new_target
                    changed = True

    return changed


def main():
    parser = argparse.ArgumentParser(description="Migrate newsletter rules to Marketing/Topics structure")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing files")
    args = parser.parse_args()

    rule_files = sorted(RULES_DIR.glob("*.json"))
    changed_files = []
    skipped = 0
    folder_renames = {}  # old → new for IMAP folder rename reference

    for path in rule_files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"  WARN: could not read {path.name}: {e}", file=sys.stderr)
            continue

        name = data.get("name", "")
        mapping = find_mapping(name)
        if not mapping:
            skipped += 1
            continue

        # Track folder renames
        _, _, target_prefix, new_target_prefix = mapping
        for action in data.get("actions", []):
            if action.get("type") == "move":
                old_t = action.get("target", "")
                if old_t.startswith(target_prefix) and old_t not in folder_renames:
                    new_t = new_target_prefix + old_t[len(target_prefix):]
                    if new_t != old_t:
                        folder_renames[old_t] = new_t

        old_name = data.get("name", "")
        changed = migrate_rule(data, mapping)
        if changed:
            changed_files.append((path, old_name, data.get("name", "")))
            if not args.dry_run:
                path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"\n{'DRY RUN — ' if args.dry_run else ''}Results:")
    print(f"  Rules updated : {len(changed_files)}")
    print(f"  Rules skipped : {skipped}")

    if changed_files:
        print("\nChanged rules:")
        for path, old_name, new_name in changed_files:
            print(f"  {path.name}")
            print(f"    {old_name}")
            print(f"    → {new_name}")

    if folder_renames:
        print(f"\nIMAP folders to rename ({len(folder_renames)}):")
        for old, new in sorted(folder_renames.items()):
            print(f"  {old}")
            print(f"  → {new}")

    if args.dry_run:
        print("\n(No files written — re-run without --dry-run to apply)")


if __name__ == "__main__":
    main()

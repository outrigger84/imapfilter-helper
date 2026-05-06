#!/usr/bin/env python3
"""
Generate IMAPFilter rule JSON files for all uncovered senders with 5+ emails.
Skips domains that are personal email providers (icloud.com, gmail.com, etc.)
or clearly personal conversations.
"""

import json
import os
import re

RULES_DIR = '/Users/stephenjgibson/imapfilter-helper/rules/'

# Starting timestamp
ts = 1746400001

def make_rule(name, target, conditions_any, priority=100, comments=None):
    return {
        "name": name,
        "enabled": True,
        "priority": priority,
        "conditions": {
            "any": conditions_any
        },
        "actions": [
            {"type": "move", "target": target},
            {"type": "set_keywords", "keywords": ["Retain365"]}
        ],
        "comments": comments or ["Created with IMAPFilter Rule Wizard"]
    }

def from_domain(domain):
    return [{"header": "from", "contains": f"@{domain}"}]

def from_address(addr):
    return [{"header": "from", "contains": addr}]

def sanitize_filename(s):
    s = s.lower()
    s = re.sub(r'[^a-z0-9_/]', '_', s)
    s = re.sub(r'_+', '_', s)
    s = s.strip('_')
    return s

# Each entry: (filename_suffix, rule_data)
# Generated from analysis of all uncovered domains with 5+ emails
# Personal/generic email providers are SKIPPED (icloud.com, gmail.com, hotmail.com,
# yahoo.com, yahoo.co.uk, btinternet.com, talktalk.net, outlook.com)
# Also skipping: domains that are clearly personal conversation threads

rules = [
    # ─── UK GOV NOTIFICATIONS ────────────────────────────────────────────────
    (
        "notifications_service_gov_uk",
        make_rule(
            "Notifications » UK Government",
            "Notifications/UK Government",
            from_domain("notifications.service.gov.uk"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Gov.uk notification service - COVID tests, flood alerts, DVSA etc"]
        )
    ),

    # ─── PARABOLA.IO - SaaS newsletters ──────────────────────────────────────
    (
        "newsletters_marketing_tech_parabola",
        make_rule(
            "Newsletters » Marketing » Tech » Parabola",
            "Newsletters/Marketing/Tech/Parabola",
            from_domain("parabola.io"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Data automation SaaS marketing emails"]
        )
    ),

    # ─── ENVIRONMENTUK.COM - personal contact ────────────────────────────────
    # Skipping - appears to be a single contact person (Tony Mayne), form replies

    # ─── XERO - account notifications ────────────────────────────────────────
    (
        "notifications_xero",
        make_rule(
            "Notifications » Xero",
            "Notifications/Xero",
            from_domain("post.xero.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Xero accounting login/security notifications"]
        )
    ),

    # ─── MARRIOTT - travel accommodation ─────────────────────────────────────
    (
        "travel_accommodation_marriott_general",
        make_rule(
            "Travel » Accommodation » Marriott",
            "Travel/Accommodation/Marriott",
            [
                {"header": "from", "contains": "@marriott.com"},
                {"header": "from", "contains": "@h1.hilton.com"},
                {"header": "from", "contains": "@h6.hilton.com"},
                {"header": "from", "contains": "@h4.hilton.com"},
                {"header": "from", "contains": "@hilton.com"},
            ],
            comments=["Created with IMAPFilter Rule Wizard",
                      "Marriott and Hilton hotel stays - already have marriott.com in travel rules but not hilton"]
        )
    ),

    # ─── LAPTOPS DIRECT - receipts retail ────────────────────────────────────
    (
        "receipts_retail_tech_laptops_direct",
        make_rule(
            "Receipts » Retail » Tech » Laptops Direct",
            "Receipts/Retail/Tech/Laptops Direct",
            from_domain("laptopsdirect.co.uk"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Laptops Direct order confirmations and refunds"]
        )
    ),

    # ─── BINGOPORT - newsletters/gambling ────────────────────────────────────
    (
        "newsletters_marketing_bingoport",
        make_rule(
            "Newsletters » Marketing » BingoPort",
            "Newsletters/Marketing/BingoPort",
            from_domain("bpuk.memberinfosupport.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "BingoPort promotional emails"]
        )
    ),

    # ─── DESRENEWABLES - property/home services ───────────────────────────────
    (
        "property_renovations_des_renewables",
        make_rule(
            "Property » Renovations » DES Renewables",
            "Property/Renovations/DES Renewables",
            from_domain("desrenewables.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "DES Renewables solar/energy installation appointments"]
        )
    ),

    # ─── WEALTHIFY - personal finance ────────────────────────────────────────
    (
        "personal_finance_wealthify",
        make_rule(
            "Personal » Finance » Wealthify",
            "Personal/Finance/Wealthify",
            [
                {"header": "from", "contains": "@wealthify.com"},
                {"header": "from", "contains": "@directdebitnotice.co.uk"},
            ],
            comments=["Created with IMAPFilter Rule Wizard",
                      "Wealthify investment account - includes directdebitnotice.co.uk domain used for DD notifications"]
        )
    ),

    # ─── NOODLESOFT - software subscriptions ─────────────────────────────────
    (
        "subscriptions_noodlesoft_hazel",
        make_rule(
            "Subscriptions » Noodlesoft Hazel",
            "Subscriptions/Noodlesoft Hazel",
            from_domain("noodlesoft.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Noodlesoft Hazel mac automation software newsletters/updates"]
        )
    ),

    # ─── STRIKINGLY - spam/phishing ──────────────────────────────────────────
    # Skipping - strikingly.com is used for phishing/spam (fake FedEx emails)

    # ─── AEGON - personal finance ─────────────────────────────────────────────
    (
        "personal_finance_aegon",
        make_rule(
            "Personal » Finance » Aegon",
            "Personal/Finance/Aegon",
            from_domain("aegon.co.uk"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Aegon pension/investment account notifications"]
        )
    ),

    # ─── THE HALL EXETER - events venue ──────────────────────────────────────
    (
        "receipts_events_the_hall_exeter",
        make_rule(
            "Receipts » Events » The Hall Exeter",
            "Receipts/Events/The Hall Exeter",
            from_domain("thehallexeter.org"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "The Hall Exeter event venue booking correspondence"]
        )
    ),

    # ─── CAFE MANGOS - restaurant ────────────────────────────────────────────
    (
        "receipts_restaurants_cafe_mangos",
        make_rule(
            "Receipts » Restaurants » Cafe Mangos",
            "Receipts/Restaurants/Cafe Mangos",
            from_domain("cafemangos.co.uk"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Cafe Mangos restaurant booking/event correspondence"]
        )
    ),

    # ─── TESCO - grocery receipts ────────────────────────────────────────────
    (
        "receipts_retail_grocery_tesco",
        make_rule(
            "Receipts » Retail » Grocery » Tesco",
            "Receipts/Retail/Grocery/Tesco",
            [
                {"header": "from", "contains": "@tesco.co.uk"},
                {"header": "from", "contains": "@customer-service.tesco.com"},
            ],
            comments=["Created with IMAPFilter Rule Wizard",
                      "Tesco online grocery order confirmations"]
        )
    ),

    # ─── AMAZON.COM - work/AWS emails ────────────────────────────────────────
    # Skipping - amazon.com here appears to be work colleague (colbor@amazon.com) not retail

    # ─── ABEBOOKS - book receipts ────────────────────────────────────────────
    (
        "receipts_retail_abebooks",
        make_rule(
            "Receipts » Retail » AbeBooks",
            "Receipts/Retail/AbeBooks",
            [
                {"header": "from", "contains": "@abebooks.com"},
                {"header": "from", "contains": "@orbitingbooks.com"},
            ],
            comments=["Created with IMAPFilter Rule Wizard",
                      "AbeBooks second-hand book orders - orbitingbooks.com is a seller shipping confirmations domain"]
        )
    ),

    # ─── PETER PAN BUS - travel ground transport ─────────────────────────────
    (
        "travel_ground_transport_peter_pan_bus",
        make_rule(
            "Travel » Ground Transport » Peter Pan Bus",
            "Travel/Ground Transport/Peter Pan Bus",
            from_domain("peterpanbus.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Peter Pan Bus Lines (New England) ticket confirmations"]
        )
    ),

    # ─── LONG ISLAND FERRY - travel ground transport ─────────────────────────
    (
        "travel_ground_transport_long_island_ferry",
        make_rule(
            "Travel » Ground Transport » Long Island Ferry",
            "Travel/Ground Transport/Long Island Ferry",
            from_domain("longislandferry.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Long Island Ferry reservation confirmations"]
        )
    ),

    # ─── WILLIAM HILL - newsletters/gambling ─────────────────────────────────
    (
        "newsletters_marketing_william_hill",
        make_rule(
            "Newsletters » Marketing » William Hill",
            "Newsletters/Marketing/William Hill",
            from_domain("my.williamhill.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "William Hill betting promotional emails"]
        )
    ),

    # ─── THE CLOUD WIFI - notifications ──────────────────────────────────────
    (
        "notifications_the_cloud_wifi",
        make_rule(
            "Notifications » The Cloud WiFi",
            "Notifications/The Cloud WiFi",
            from_domain("thecloud.net"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "The Cloud pay-as-you-go WiFi login/order details"]
        )
    ),

    # ─── CURIOSITYSTREAM - subscriptions ─────────────────────────────────────
    (
        "subscriptions_curiositystream",
        make_rule(
            "Subscriptions » CuriosityStream",
            "Subscriptions/CuriosityStream",
            from_domain("curiositystream.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "CuriosityStream documentary streaming subscription"]
        )
    ),

    # ─── TINDER/GOTINDER - support ───────────────────────────────────────────
    (
        "support_tinder",
        make_rule(
            "Support » Tinder",
            "Support/Tinder",
            [
                {"header": "from", "contains": "@gotinder.com"},
                {"header": "from", "contains": "@us1-khalidit.host4speed.com"},
            ],
            comments=["Created with IMAPFilter Rule Wizard",
                      "Tinder support emails and match notifications (gotinder.com + obfuscated domain)"]
        )
    ),

    # ─── TILE - notifications ─────────────────────────────────────────────────
    (
        "notifications_tile",
        make_rule(
            "Notifications » Tile",
            "Notifications/Tile",
            [
                {"header": "from", "contains": "@email.tile.com"},
                {"header": "from", "contains": "@account.tile.com"},
            ],
            comments=["Created with IMAPFilter Rule Wizard",
                      "Tile Bluetooth tracker account and policy notifications"]
        )
    ),

    # ─── MICROSOFT - notifications ───────────────────────────────────────────
    (
        "notifications_microsoft_onedrive",
        make_rule(
            "Notifications » Microsoft",
            "Notifications/Microsoft",
            [
                {"header": "from", "contains": "@microsoft.com"},
                {"header": "from", "contains": "@sharepointonline.com"},
                {"header": "from", "contains": "@notify.microsoft.com"},
                {"header": "from", "contains": "@communication.microsoft.com"},
                {"header": "from", "contains": "@engage.windows.com"},
                {"header": "from", "contains": "@e-mail.microsoft.com"},
            ],
            comments=["Created with IMAPFilter Rule Wizard",
                      "Microsoft/OneDrive/SharePoint account notifications and guest receipts"]
        )
    ),

    # ─── CHATFUEL - notifications ─────────────────────────────────────────────
    (
        "notifications_chatfuel",
        make_rule(
            "Notifications » Chatfuel",
            "Notifications/Chatfuel",
            from_domain("mail.chatfuel.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Chatfuel chatbot platform notifications"]
        )
    ),

    # ─── VIRGIN HOLIDAYS AGENT - travel ──────────────────────────────────────
    (
        "travel_agents_virgin_holidays",
        make_rule(
            "Travel » Agents » Virgin Holidays",
            "Travel/Agents/Virgin Holidays",
            [
                {"header": "from", "contains": "@fly.virgin.com"},
                {"header": "from", "contains": "@emails.virginholidays.co.uk"},
                {"header": "from", "contains": "@noreply.red.virgin.com"},
                {"header": "from", "contains": "@noreply.account.virgin.com"},
                {"header": "from", "contains": "@notifications.virginmedia.co.uk"},
            ],
            comments=["Created with IMAPFilter Rule Wizard",
                      "Virgin Holidays/Media/Red - various Virgin branded email domains"]
        )
    ),

    # ─── TRUSTEDSHOPS - notifications ────────────────────────────────────────
    (
        "notifications_trusted_shops",
        make_rule(
            "Notifications » Trusted Shops",
            "Notifications/Trusted Shops",
            from_domain("trustedshops.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Trusted Shops buyer protection notifications"]
        )
    ),

    # ─── WALMART - newsletters ───────────────────────────────────────────────
    (
        "newsletters_marketing_walmart",
        make_rule(
            "Newsletters » Marketing » Walmart",
            "Newsletters/Marketing/Walmart",
            from_domain("em.walmart.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Walmart marketing and terms updates"]
        )
    ),

    # ─── ME&U - restaurant receipts ──────────────────────────────────────────
    (
        "receipts_restaurants_meandu",
        make_rule(
            "Receipts » Restaurants » me&u",
            "Receipts/Restaurants/me&u",
            from_domain("meandu.co.uk"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "me&u mobile ordering receipts (The Botanist etc.)"]
        )
    ),

    # ─── MIXERGY - newsletters ───────────────────────────────────────────────
    (
        "newsletters_marketing_mixergy",
        make_rule(
            "Newsletters » Marketing » Mixergy",
            "Newsletters/Marketing/Mixergy",
            from_domain("mixergy.co.uk"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Mixergy hot water tank community newsletter"]
        )
    ),

    # ─── HOUSE SALES DIRECT - newsletters ────────────────────────────────────
    (
        "newsletters_marketing_house_sales_direct",
        make_rule(
            "Newsletters » Marketing » House Sales Direct",
            "Newsletters/Marketing/House Sales Direct",
            from_domain("housesalesdirect.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "House Sales Direct property investment marketing emails"]
        )
    ),

    # ─── THERABODY - receipts retail ─────────────────────────────────────────
    (
        "receipts_retail_therabody",
        make_rule(
            "Receipts » Retail » Therabody",
            "Receipts/Retail/Therabody",
            from_domain("t.uk.therabody.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Therabody (Theragun) order confirmations"]
        )
    ),

    # ─── BRITISH LIBRARY - newsletters ───────────────────────────────────────
    (
        "newsletters_marketing_british_library",
        make_rule(
            "Newsletters » Marketing » British Library",
            "Newsletters/Marketing/British Library",
            from_domain("servicemessage.bl.uk"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "British Library Business & IP Centre newsletters"]
        )
    ),

    # ─── DIONYSOS ZONARS - restaurant (Greece) ───────────────────────────────
    (
        "receipts_restaurants_dionysos_zonars",
        make_rule(
            "Receipts » Restaurants » Dionysos Zonars",
            "Receipts/Restaurants/Dionysos Zonars",
            from_domain("dionysoszonars.gr"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Dionysos Zonars Athens restaurant reservation correspondence"]
        )
    ),

    # ─── UNIDAYS - notifications ──────────────────────────────────────────────
    (
        "notifications_unidays",
        make_rule(
            "Notifications » UNiDAYS",
            "Notifications/UNiDAYS",
            from_domain("myunidays.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "UNiDAYS student discount platform account notifications"]
        )
    ),

    # ─── AXS - event tickets ─────────────────────────────────────────────────
    (
        "receipts_events_axs",
        make_rule(
            "Receipts » Events » AXS",
            "Receipts/Events/AXS",
            [
                {"header": "from", "contains": "@axs.com"},
                {"header": "from", "contains": "@boxoffice.axs.co.uk"},
                {"header": "from", "contains": "@axs.zendesk.com"},
            ],
            comments=["Created with IMAPFilter Rule Wizard",
                      "AXS event ticket purchases and account notifications"]
        )
    ),

    # ─── BREWDOG NOW (HUNGRRR) - restaurant receipts ──────────────────────────
    (
        "receipts_restaurants_brewdog",
        make_rule(
            "Receipts » Restaurants » BrewDog",
            "Receipts/Restaurants/BrewDog",
            from_domain("hungrrr.co.uk"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "BrewDog Now food delivery order receipts (uses hungrrr.co.uk)"]
        )
    ),

    # ─── COOK FOOD - receipts retail food ────────────────────────────────────
    (
        "receipts_retail_food_cook",
        make_rule(
            "Receipts » Retail » Food » COOK",
            "Receipts/Retail/Food/COOK",
            from_domain("cookfood.net"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "COOK frozen meals order confirmations"]
        )
    ),

    # ─── HUME HEALTH - health services ───────────────────────────────────────
    (
        "health_services_hume_health",
        make_rule(
            "Health » Services » Hume Health",
            "Health/Services/Hume Health",
            from_domain("myhumehealth.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Hume Health health monitoring service"]
        )
    ),

    # ─── SCHED.COM - event/conference schedules ───────────────────────────────
    (
        "notifications_sched",
        make_rule(
            "Notifications » Sched",
            "Notifications/Sched",
            from_domain("sched.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Sched event schedule platform notifications (Comic-Con etc.)"]
        )
    ),

    # ─── BITNAMI/BROADCOM - server/tech newsletters ───────────────────────────
    (
        "newsletters_marketing_tech_bitnami",
        make_rule(
            "Newsletters » Marketing » Tech » Bitnami",
            "Newsletters/Marketing/Tech/Bitnami",
            [
                {"header": "from", "contains": "@bitnami.com"},
                {"header": "from", "contains": "@broadcom.com"},
            ],
            comments=["Created with IMAPFilter Rule Wizard",
                      "Bitnami (now Broadcom) application packaging newsletters"]
        )
    ),

    # ─── DELIGHTED - feedback surveys ────────────────────────────────────────
    (
        "feedback_delighted",
        make_rule(
            "Feedback » Delighted",
            "Feedback/Delighted",
            from_domain("delighted.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Delighted NPS/survey platform used by Linode, Strava etc."]
        )
    ),

    # ─── KICKSTARTREND/BACKERTREND - newsletters ──────────────────────────────
    (
        "newsletters_marketing_tech_backertrend",
        make_rule(
            "Newsletters » Marketing » Tech » BackerTrend",
            "Newsletters/Marketing/Tech/BackerTrend",
            from_domain("backertrend.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "KickstarTrend/BackerTrend crowdfunding newsletter"]
        )
    ),

    # ─── FREEPRINTS - receipts retail ────────────────────────────────────────
    (
        "receipts_retail_freeprints",
        make_rule(
            "Receipts » Retail » FreePrints",
            "Receipts/Retail/FreePrints",
            from_domain("freeprintsapp.co.uk"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "FreePrints photo print order confirmations"]
        )
    ),

    # ─── THE GYM GROUP - fitness ──────────────────────────────────────────────
    (
        "fitness_the_gym_group",
        make_rule(
            "Fitness » The Gym Group",
            "Fitness/The Gym Group",
            from_domain("thegymgroup.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "The Gym Group membership billing and notifications"]
        )
    ),

    # ─── BEER52 SUPPORT - support ────────────────────────────────────────────
    (
        "support_beer52",
        make_rule(
            "Support » Beer52",
            "Support/Beer52",
            from_domain("beer52.freshdesk.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Beer52/Wine52 subscription box customer support"]
        )
    ),

    # ─── FLEXX MEMORY - receipts retail ──────────────────────────────────────
    (
        "receipts_retail_tech_flexx_memory",
        make_rule(
            "Receipts » Retail » Tech » Flexx Memory",
            "Receipts/Retail/Tech/Flexx Memory",
            from_domain("flexxmemory.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Flexx Memory (RAM/storage) order confirmations"]
        )
    ),

    # ─── BIKRAM YOGA LONDON - fitness ────────────────────────────────────────
    (
        "fitness_bikram_yoga_london",
        make_rule(
            "Fitness » Bikram Yoga London",
            "Fitness/Bikram Yoga London",
            from_domain("bikramyogalondon.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Bikram Yoga London / Hot Yoga Unity newsletters and offers"]
        )
    ),

    # ─── PLAY.COM - receipts retail ──────────────────────────────────────────
    (
        "receipts_retail_play_com",
        make_rule(
            "Receipts » Retail » Play.com",
            "Receipts/Retail/Play.com",
            from_domain("play.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Play.com (now Rakuten) order confirmations (older emails)"]
        )
    ),

    # ─── PRIORITY PASS - travel ───────────────────────────────────────────────
    (
        "travel_priority_pass",
        make_rule(
            "Travel » Priority Pass",
            "Travel/Priority Pass",
            from_domain("email.prioritypass.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Priority Pass airport lounge membership notifications"]
        )
    ),

    # ─── EXETER LEISURE (SERVICETSG) - fitness ────────────────────────────────
    (
        "fitness_exeter_leisure",
        make_rule(
            "Fitness » Exeter Leisure",
            "Fitness/Exeter Leisure",
            from_domain("servicetsg.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Exeter Leisure / Riverside Leisure Centre membership emails (uses servicetsg.com)"]
        )
    ),

    # ─── FLUID APP - software subscriptions ──────────────────────────────────
    (
        "subscriptions_fluid_app",
        make_rule(
            "Subscriptions » Fluid App",
            "Subscriptions/Fluid App",
            from_domain("fluidapp.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Fluid mac app license / subscription emails"]
        )
    ),

    # ─── SHAREIT/MYCOMMERCE - software receipts ───────────────────────────────
    (
        "receipts_services_shareit",
        make_rule(
            "Receipts » Services » ShareIt",
            "Receipts/Services/ShareIt",
            from_domain("shareit.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Share-it! / MyCommerce software purchase delivery notifications"]
        )
    ),

    # ─── RICHARD MILLINGTON - newsletters ────────────────────────────────────
    (
        "newsletters_marketing_richard_millington",
        make_rule(
            "Newsletters » Marketing » Richard Millington",
            "Newsletters/Marketing/Richard Millington",
            from_domain("richardmillington.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Richard Millington / Indispensable Consulting community building newsletter"]
        )
    ),

    # ─── UBER - notifications ─────────────────────────────────────────────────
    (
        "notifications_uber",
        make_rule(
            "Notifications » Uber",
            "Notifications/Uber",
            from_domain("uber.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Uber account security notifications"]
        )
    ),

    # ─── SIMPLYHEALTH - health services ──────────────────────────────────────
    (
        "health_services_simplyhealth",
        make_rule(
            "Health » Services » Simplyhealth",
            "Health/Services/Simplyhealth",
            [
                {"header": "from", "contains": "@m.simplyhealth.co.uk"},
                {"header": "from", "contains": "@customerservices.simplyhealth.co.uk"},
            ],
            comments=["Created with IMAPFilter Rule Wizard",
                      "Simplyhealth dental/health plan benefit reminders and account updates"]
        )
    ),

    # ─── DRIVEWEALTH - personal finance ──────────────────────────────────────
    (
        "personal_finance_drivewealth",
        make_rule(
            "Personal » Finance » DriveWealth",
            "Personal/Finance/DriveWealth",
            from_domain("drivewealth.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "DriveWealth (Revolut broker) important investment disclosures"]
        )
    ),

    # ─── NEW SCIENTIST (PROCESSREQUEST) - newsletters ─────────────────────────
    (
        "newsletters_marketing_new_scientist",
        make_rule(
            "Newsletters » Marketing » New Scientist",
            "Newsletters/Marketing/New Scientist",
            from_domain("processrequest.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "New Scientist newsletter (uses processrequest.com email domain)"]
        )
    ),

    # ─── STAR LETTINGS - receipts/property ───────────────────────────────────
    (
        "receipts_services_star_lettings",
        make_rule(
            "Receipts » Services » Star Lettings",
            "Receipts/Services/Star Lettings",
            from_domain("star-students.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Star Lettings & Property Management rental receipts"]
        )
    ),

    # ─── HSBC - personal finance banking ─────────────────────────────────────
    (
        "personal_finance_hsbc",
        make_rule(
            "Personal » Finance » HSBC",
            "Personal/Finance/HSBC",
            from_domain("hsbc.co.uk"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "HSBC UK banking app and account notifications"]
        )
    ),

    # ─── CHIP (INTERCOM) - personal finance ──────────────────────────────────
    (
        "support_chip",
        make_rule(
            "Support » Chip",
            "Support/Chip",
            from_domain("chip-6f6bcffeba9b.intercom-mail.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Chip savings app support via Intercom"]
        )
    ),

    # ─── FAST RAIL TICKETING - travel ────────────────────────────────────────
    (
        "travel_ground_transport_fastrail",
        make_rule(
            "Travel » Ground Transport » FastRail",
            "Travel/Ground Transport/FastRail",
            from_domain("fastrailticketing.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Fast Rail Ticketing train ticket refund claims"]
        )
    ),

    # ─── HOWDENS - property renovations ──────────────────────────────────────
    (
        "property_renovations_howdens",
        make_rule(
            "Property » Renovations » Howdens",
            "Property/Renovations/Howdens",
            from_domain("howdens.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Howdens kitchen/joinery trade supplier orders"]
        )
    ),

    # ─── ENVIRONMENT AGENCY - notifications ──────────────────────────────────
    (
        "notifications_environment_agency",
        make_rule(
            "Notifications » Environment Agency",
            "Notifications/Environment Agency",
            from_domain("environment-agency.gov.uk"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Environment Agency flood warning account notifications"]
        )
    ),

    # ─── VIRGIN ATLANTIC - travel flights ────────────────────────────────────
    (
        "travel_flights_virgin_atlantic_service",
        make_rule(
            "Travel » Flights » Virgin Atlantic",
            "Travel/Flights/Virgin Atlantic",
            from_domain("service.virginatlantic.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Virgin Atlantic flight service emails (meal pre-orders etc.)"]
        )
    ),

    # ─── DISNEY INTERNATIONAL / DISNEY HOLIDAYS - travel ─────────────────────
    (
        "travel_agents_disney",
        make_rule(
            "Travel » Agents » Disney",
            "Travel/Agents/Disney",
            [
                {"header": "from", "contains": "@disneyinternational.com"},
                {"header": "from", "contains": "@disneyholidays.co.uk"},
                {"header": "from", "contains": "@mail.disney.co.uk"},
            ],
            comments=["Created with IMAPFilter Rule Wizard",
                      "Walt Disney Travel Company holiday invoices and booking confirmations"]
        )
    ),

    # ─── DIRECT DEBIT NOTICE (WEALTHIFY) - already handled with wealthify.com above

    # ─── PULLO - gifts ────────────────────────────────────────────────────────
    (
        "receipts_services_pullo",
        make_rule(
            "Receipts » Services » Pullo",
            "Receipts/Services/Pullo",
            from_domain("pullo.shop"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Pullo gift card platform notifications"]
        )
    ),

    # ─── DOMESTIQ - newsletters ───────────────────────────────────────────────
    (
        "newsletters_marketing_domestiq",
        make_rule(
            "Newsletters » Marketing » Domestiq",
            "Newsletters/Marketing/Domestiq",
            from_domain("domestiq.net"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Domestiq home management platform newsletter/updates"]
        )
    ),

    # ─── ONA.COM - newsletters tech ──────────────────────────────────────────
    (
        "newsletters_marketing_tech_ona",
        make_rule(
            "Newsletters » Marketing » Tech » Ona",
            "Newsletters/Marketing/Tech/Ona",
            from_domain("ona.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Ona data platform tech newsletter"]
        )
    ),

    # ─── MOPAK - travel app ───────────────────────────────────────────────────
    (
        "travel_mopak",
        make_rule(
            "Travel » Mopak",
            "Travel/Mopak",
            from_domain("mopak.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Mopak multi-modal travel app offers and promotions"]
        )
    ),

    # ─── OPENTABLE - restaurant bookings ─────────────────────────────────────
    (
        "receipts_restaurants_opentable",
        make_rule(
            "Receipts » Restaurants » OpenTable",
            "Receipts/Restaurants/OpenTable",
            from_domain("opentable.co.uk"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "OpenTable restaurant reservation confirmations and cancellations"]
        )
    ),

    # ─── THE BRUNSWICK (ALMARKETING) - newsletters ────────────────────────────
    (
        "newsletters_marketing_the_brunswick",
        make_rule(
            "Newsletters » Marketing » The Brunswick",
            "Newsletters/Marketing/The Brunswick",
            from_domain("almarketing.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "The Brunswick (pub/venue) events newsletter via almarketing.com"]
        )
    ),

    # ─── DASHLANE - notifications (multiple spam domains) ────────────────────
    (
        "notifications_dashlane",
        make_rule(
            "Notifications » Dashlane",
            "Notifications/Dashlane",
            [
                {"header": "from", "contains": "@supercolonial.com"},
                {"header": "from", "contains": "@ryt.email"},
                {"header": "from", "contains": "@aguerovera.com.ar"},
            ],
            comments=["Created with IMAPFilter Rule Wizard",
                      "Dashlane password manager account notifications (sent from unusual relay domains)"]
        )
    ),

    # ─── O2 ACADEMY VENUES - newsletters entertainment ───────────────────────
    (
        "newsletters_entertainment_o2_academy",
        make_rule(
            "Newsletters » Entertainment » O2 Academy",
            "Newsletters/Entertainment/O2 Academy",
            from_domain("info.academy-music-group.co.uk"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "O2 Academy / Academy Music Group venue gig guides"]
        )
    ),

    # ─── PAPA JOHN'S - receipts restaurants (additional domain) ──────────────
    (
        "receipts_restaurants_papa_johns_email",
        make_rule(
            "Receipts » Restaurants » Papa John's",
            "Receipts/Restaurants/Papa John's",
            from_domain("e-papajohns.co.uk"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Papa John's marketing and offers emails (e-papajohns.co.uk subdomain)"]
        )
    ),

    # ─── NATALIE SISSON / PODIA - newsletters ────────────────────────────────
    (
        "newsletters_marketing_podia",
        make_rule(
            "Newsletters » Marketing » Podia",
            "Newsletters/Marketing/Podia",
            from_domain("e.podia.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Natalie Sisson / Podia platform marketing newsletter"]
        )
    ),

    # ─── PSYCLE LONDON - fitness ──────────────────────────────────────────────
    (
        "fitness_psycle_london",
        make_rule(
            "Fitness » Psycle London",
            "Fitness/Psycle London",
            from_domain("psyclelondon.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Psycle London cycling studio subscription confirmation"]
        )
    ),

    # ─── GUESTLINE / POINT A HOTEL - travel accommodation ────────────────────
    (
        "travel_accommodation_guestline",
        make_rule(
            "Travel » Accommodation » Point A Hotel",
            "Travel/Accommodation/Point A Hotel",
            from_domain("guestline.net"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Point A Hotel (via Guestline PMS) digital registration cards"]
        )
    ),

    # ─── ALDERMORE SAVINGS - personal finance ────────────────────────────────
    (
        "personal_finance_aldermore",
        make_rule(
            "Personal » Finance » Aldermore Savings",
            "Personal/Finance/Aldermore Savings",
            from_domain("aldermoresavings.co.uk"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Aldermore savings account opening notifications"]
        )
    ),

    # ─── TOPTRACER - fitness/golf ─────────────────────────────────────────────
    (
        "fitness_toptracer",
        make_rule(
            "Fitness » Toptracer",
            "Fitness/Toptracer",
            from_domain("toptracer.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Toptracer driving range golf tracking app"]
        )
    ),

    # ─── MENTION ME (HUEL REFERRAL) - newsletters ────────────────────────────
    (
        "newsletters_marketing_mention_me",
        make_rule(
            "Newsletters » Marketing » Mention Me",
            "Newsletters/Marketing/Mention Me",
            from_domain("mention-me.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Mention Me referral platform (used by Huel etc.)"]
        )
    ),

    # ─── QUIDCO - cashback/finance ────────────────────────────────────────────
    (
        "personal_finance_quidco",
        make_rule(
            "Personal » Finance » Quidco",
            "Personal/Finance/Quidco",
            from_domain("info.quidco.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Quidco cashback tracking notifications"]
        )
    ),

    # ─── THE IVY COLLECTION - newsletters food ───────────────────────────────
    (
        "newsletters_food_the_ivy",
        make_rule(
            "Newsletters » Food » The Ivy Collection",
            "Newsletters/Food/The Ivy Collection",
            from_domain("news.ivycollection.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "The Ivy Collection restaurant group newsletter"]
        )
    ),

    # ─── CASTLE GALLERIES - newsletters ──────────────────────────────────────
    (
        "newsletters_marketing_castle_galleries",
        make_rule(
            "Newsletters » Marketing » Castle Galleries",
            "Newsletters/Marketing/Castle Galleries",
            from_domain("castlegalleries.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Castle Galleries art prints promotional emails"]
        )
    ),

    # ─── HUEL - newsletters food/retail ──────────────────────────────────────
    (
        "newsletters_retail_huel",
        make_rule(
            "Newsletters » Retail » Huel",
            "Newsletters/Retail/Huel",
            from_domain("emailservice.huel.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Huel nutrition product marketing and customer updates"]
        )
    ),

    # ─── PINTEREST - notifications ────────────────────────────────────────────
    (
        "notifications_pinterest",
        make_rule(
            "Notifications » Pinterest",
            "Notifications/Pinterest",
            from_domain("explore.pinterest.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Pinterest policy updates and recommendations"]
        )
    ),

    # ─── CATALONIA REWARDS - travel ──────────────────────────────────────────
    (
        "travel_accommodation_catalonia_hotels",
        make_rule(
            "Travel » Accommodation » Catalonia Hotels",
            "Travel/Accommodation/Catalonia Hotels",
            from_domain("nw.cataloniarewards.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Catalonia Hotels loyalty rewards program"]
        )
    ),

    # ─── POLICE.UK - notifications ────────────────────────────────────────────
    (
        "notifications_police_uk",
        make_rule(
            "Notifications » Police UK",
            "Notifications/Police UK",
            from_domain("service.police.uk"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "UK Police service form submission acknowledgements"]
        )
    ),

    # ─── WIKIMEDIA / WIKIPEDIA - charity ─────────────────────────────────────
    (
        "charity_wikimedia",
        make_rule(
            "Charity » Wikimedia",
            "Charity/Wikimedia",
            from_domain("wikimedia.org"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Wikimedia Foundation donation appeals"]
        )
    ),

    # ─── SPOTIFY - notifications ──────────────────────────────────────────────
    (
        "notifications_spotify",
        make_rule(
            "Notifications » Spotify",
            "Notifications/Spotify",
            from_domain("spotify.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Spotify account security/password reset notifications"]
        )
    ),

    # ─── SURVEY MONKEY USER - feedback ───────────────────────────────────────
    (
        "feedback_survey_monkey",
        make_rule(
            "Feedback » SurveyMonkey",
            "Feedback/SurveyMonkey",
            from_domain("surveymonkeyuser.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "SurveyMonkey survey invitations sent by various businesses"]
        )
    ),

    # ─── BRITISH AIRWAYS - travel flights ────────────────────────────────────
    (
        "travel_flights_british_airways",
        make_rule(
            "Travel » Flights » British Airways",
            "Travel/Flights/British Airways",
            [
                {"header": "from", "contains": "@ba.com"},
                {"header": "from", "contains": "@account.avios.com"},
                {"header": "from", "contains": "@avios.com"},
            ],
            comments=["Created with IMAPFilter Rule Wizard",
                      "British Airways account security and Avios loyalty program"]
        )
    ),

    # ─── TRAINING PEAKS - fitness ─────────────────────────────────────────────
    (
        "fitness_training_peaks",
        make_rule(
            "Fitness » TrainingPeaks",
            "Fitness/TrainingPeaks",
            from_domain("email.trainingpeaks.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "TrainingPeaks training platform policy/terms notifications"]
        )
    ),

    # ─── SWIFTAID - charity/gift aid ─────────────────────────────────────────
    (
        "charity_swiftaid",
        make_rule(
            "Charity » Swiftaid",
            "Charity/Swiftaid",
            from_domain("swiftaid.co.uk"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Swiftaid automated Gift Aid statements"]
        )
    ),

    # ─── LARGE OUTDOORS - travel ──────────────────────────────────────────────
    (
        "travel_agents_large_outdoors",
        make_rule(
            "Travel » Agents » Large Outdoors",
            "Travel/Agents/Large Outdoors",
            from_domain("largeoutdoors.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Large Outdoors tailored adventure trip correspondence"]
        )
    ),

    # ─── OKENDO - feedback reviews ────────────────────────────────────────────
    (
        "feedback_okendo",
        make_rule(
            "Feedback » Okendo",
            "Feedback/Okendo",
            from_domain("okendo.io"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Okendo product review requests (Orbitkey etc.)"]
        )
    ),

    # ─── BOOKING.COM SUPPORT - travel ────────────────────────────────────────
    (
        "support_booking_com",
        make_rule(
            "Support » Booking.com",
            "Support/Booking.com",
            from_domain("support.booking.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Booking.com customer service support threads"]
        )
    ),

    # ─── PRONTO BIKE SHARE - travel ───────────────────────────────────────────
    (
        "travel_ground_transport_pronto",
        make_rule(
            "Travel » Ground Transport » Pronto",
            "Travel/Ground Transport/Pronto",
            from_domain("ridepronto.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Pronto bike share (Seattle) card registration"]
        )
    ),

    # ─── BILLING NOTIFICATION (HIGHGRADE COMICS) - receipts ──────────────────
    (
        "receipts_retail_highgrade_comics",
        make_rule(
            "Receipts » Retail » Highgrade Comics",
            "Receipts/Retail/Highgrade Comics",
            from_domain("billing-notification.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Highgrade Comics transaction receipts (via billing-notification.com)"]
        )
    ),

    # ─── CEWE / BOOTS PHOTO - receipts retail ────────────────────────────────
    (
        "receipts_retail_boots_photo",
        make_rule(
            "Receipts » Retail » Boots Photo",
            "Receipts/Retail/Boots Photo",
            [
                {"header": "from", "contains": "@cewe.co.uk"},
                {"header": "from", "contains": "@bootsphoto.com"},
            ],
            comments=["Created with IMAPFilter Rule Wizard",
                      "Boots Photo printing service (uses cewe.co.uk and bootsphoto.com)"]
        )
    ),

    # ─── SHAREPOINTONLINE - already handled in microsoft rule above

    # ─── CURVE - personal finance ─────────────────────────────────────────────
    (
        "personal_finance_curve",
        make_rule(
            "Personal » Finance » Curve",
            "Personal/Finance/Curve",
            from_domain("curve.app"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Curve card account verification and notifications"]
        )
    ),

    # ─── ARRYVED (HARLAND BREWING) - receipts restaurants ────────────────────
    (
        "receipts_restaurants_harland_brewing",
        make_rule(
            "Receipts » Restaurants » Harland Brewing",
            "Receipts/Restaurants/Harland Brewing",
            from_domain("arryved.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Harland Brewing bar tab receipts via Arryved POS"]
        )
    ),

    # ─── MONEYWIZ - subscriptions ─────────────────────────────────────────────
    (
        "subscriptions_moneywiz",
        make_rule(
            "Subscriptions » MoneyWiz",
            "Subscriptions/MoneyWiz",
            from_domain("wiz.money"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "MoneyWiz personal finance app subscription offers"]
        )
    ),

    # ─── BREEZE AIRWAYS - travel flights ─────────────────────────────────────
    (
        "travel_flights_breeze_airways",
        make_rule(
            "Travel » Flights » Breeze Airways",
            "Travel/Flights/Breeze Airways",
            from_domain("flybreeze.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Breeze Airways promotional flight deals"]
        )
    ),

    # ─── MAGIC TRAVEL AI - travel ────────────────────────────────────────────
    (
        "travel_agents_magic_travel",
        make_rule(
            "Travel » Agents » Magic Travel",
            "Travel/Agents/Magic Travel",
            from_domain("magictravel.ai"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Magic Travel AI travel planning service onboarding"]
        )
    ),

    # ─── LYFT - travel ground transport ──────────────────────────────────────
    (
        "travel_ground_transport_lyft",
        make_rule(
            "Travel » Ground Transport » Lyft",
            "Travel/Ground Transport/Lyft",
            from_domain("lyftmail.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Lyft ride-sharing marketing and account emails"]
        )
    ),

    # ─── DOMINO'S PIZZA - newsletters/marketing ──────────────────────────────
    (
        "newsletters_marketing_food_dominos",
        make_rule(
            "Newsletters » Marketing » Food » Domino's",
            "Newsletters/Marketing/Food/Domino's",
            [
                {"header": "from", "contains": "@email.dominosmarketing.co.uk"},
                {"header": "from", "contains": "@feedback.dominosmarketing.co.uk"},
            ],
            comments=["Created with IMAPFilter Rule Wizard",
                      "Domino's Pizza marketing emails and privacy policy updates"]
        )
    ),

    # ─── HP INSTANT INK - subscriptions ──────────────────────────────────────
    (
        "subscriptions_hp_instant_ink",
        make_rule(
            "Subscriptions » HP Instant Ink",
            "Subscriptions/HP Instant Ink",
            from_domain("hp.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "HP Instant Ink printer subscription account notifications"]
        )
    ),

    # ─── SKY GIFTCLOUD - receipts/services ───────────────────────────────────
    (
        "receipts_services_giftcloud",
        make_rule(
            "Receipts » Services » Giftcloud",
            "Receipts/Services/Giftcloud",
            from_domain("giftcloud.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Sky Broadband gift/reward notifications via Giftcloud"]
        )
    ),

    # ─── AIRALO - travel eSIM ────────────────────────────────────────────────
    (
        "travel_airalo",
        make_rule(
            "Travel » Airalo",
            "Travel/Airalo",
            from_domain("airalo.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Airalo eSIM travel data purchase confirmations"]
        )
    ),

    # ─── STANSTED EXPRESS/AIRPORT - travel ───────────────────────────────────
    (
        "travel_ground_transport_stansted",
        make_rule(
            "Travel » Ground Transport » Stansted Express",
            "Travel/Ground Transport/Stansted Express",
            from_domain("stanstedairport.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Stansted Airport/Express rail booking confirmations"]
        )
    ),

    # ─── WAGAMAMA FEEDBACK - feedback ────────────────────────────────────────
    (
        "feedback_wagamama",
        make_rule(
            "Feedback » Wagamama",
            "Feedback/Wagamama",
            from_domain("mailer.wagamama.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Wagamama restaurant feedback/survey requests"]
        )
    ),

    # ─── ICOMERA (TRAIN WIFI) - notifications ────────────────────────────────
    (
        "notifications_icomera_train_wifi",
        make_rule(
            "Notifications » Icomera Train WiFi",
            "Notifications/Icomera Train WiFi",
            from_domain("icomera.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Icomera Northern train WiFi validation emails"]
        )
    ),

    # ─── SOUTH WEST WATER (ESAYWORK) - property/services ─────────────────────
    (
        "property_renovations_south_west_water",
        make_rule(
            "Property » Renovations » South West Water",
            "Property/Renovations/South West Water",
            from_domain("esayworkmobile.co.uk"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "South West Water customer pipework replacement agreements (via eSayWork)"]
        )
    ),

    # ─── PODBACK - receipts retail ────────────────────────────────────────────
    (
        "receipts_retail_podback",
        make_rule(
            "Receipts » Retail » Podback",
            "Receipts/Retail/Podback",
            from_domain("podback.org"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Podback coffee pod recycling scheme order confirmations"]
        )
    ),

    # ─── ECARD FOREST - subscriptions ────────────────────────────────────────
    (
        "subscriptions_ecardforest",
        make_rule(
            "Subscriptions » EcardForest",
            "Subscriptions/EcardForest",
            from_domain("ecardforest.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "EcardForest digital greeting card service"]
        )
    ),

    # ─── EXETER UNIVERSITY - notifications ───────────────────────────────────
    (
        "notifications_university_of_exeter",
        make_rule(
            "Notifications » University of Exeter",
            "Notifications/University of Exeter",
            from_domain("exeter.ac.uk"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "University of Exeter graduation assistance and staff correspondence"]
        )
    ),

    # ─── CRACKING ENERGY (SOLAR) - property renovations ──────────────────────
    (
        "property_renovations_cracking_energy",
        make_rule(
            "Property » Renovations » Cracking Energy",
            "Property/Renovations/Cracking Energy",
            from_domain("crackingenergy.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Cracking Energy solar installation quotes"]
        )
    ),

    # ─── SENDCLOUD - receipts retail ─────────────────────────────────────────
    (
        "receipts_retail_sendcloud",
        make_rule(
            "Receipts » Retail » Sendcloud",
            "Receipts/Retail/Sendcloud",
            from_domain("sendcloud.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Sendcloud shipping platform delivery notifications"]
        )
    ),

    # ─── TEXTME / GO-TEXT - notifications ────────────────────────────────────
    (
        "notifications_textme",
        make_rule(
            "Notifications » TextMe",
            "Notifications/TextMe",
            from_domain("go-text.me"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "TextMe app welcome and account notifications"]
        )
    ),

    # ─── LODGIFY - travel/accommodation ──────────────────────────────────────
    (
        "travel_accommodation_lodgify",
        make_rule(
            "Travel » Accommodation » Lodgify",
            "Travel/Accommodation/Lodgify",
            from_domain("messaging.lodgify.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Lodgify vacation rental property reviews (via Karl Wakefield property)"]
        )
    ),

    # ─── EXPEDIA AUTH - travel ────────────────────────────────────────────────
    (
        "notifications_expedia",
        make_rule(
            "Notifications » Expedia",
            "Notifications/Expedia",
            from_domain("accounts.expedia.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Expedia account sign-in verification codes"]
        )
    ),

    # ─── MEGABUS - travel ground transport ───────────────────────────────────
    (
        "travel_ground_transport_megabus",
        make_rule(
            "Travel » Ground Transport » Megabus",
            "Travel/Ground Transport/Megabus",
            from_domain("megabus.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Megabus/Stagecoach coach ticket reservations"]
        )
    ),

    # ─── THE LEAGUE DATING APP - notifications ────────────────────────────────
    (
        "notifications_the_league",
        make_rule(
            "Notifications » The League",
            "Notifications/The League",
            from_domain("theleague.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "The League dating app notifications"]
        )
    ),

    # ─── JET2 HOLIDAYS - travel ───────────────────────────────────────────────
    (
        "travel_agents_jet2_holidays",
        make_rule(
            "Travel » Agents » Jet2holidays",
            "Travel/Agents/Jet2holidays",
            from_domain("jet2holidaysemail.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Jet2holidays marketing deals and offers"]
        )
    ),

    # ─── EDISON MAIL - subscriptions ─────────────────────────────────────────
    (
        "subscriptions_edison_mail",
        make_rule(
            "Subscriptions » Edison Mail",
            "Subscriptions/Edison Mail",
            from_domain("edisonmail.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Edison Mail app welcome and subscription notifications"]
        )
    ),

    # ─── SMARTYPLANTS - subscriptions/tech ───────────────────────────────────
    (
        "subscriptions_smartyplants",
        make_rule(
            "Subscriptions » SmartyPlants",
            "Subscriptions/SmartyPlants",
            from_domain("smartyplants.ai"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "SmartyPlants AI plant monitoring sensor updates"]
        )
    ),

    # ─── SPAM/PHISHING DOMAINS → DELETE ──────────────────────────────────────
    # strikingly.com, bz04.plala.or.jp, casa-verano-eterno.be, sofinn.it,
    # starazagora.bg, lytyrjcgzdfyvtheskimm.com, moinabypafcxotheskimm.com,
    # mac.europiumuk.com, 57145307688512.cgaux.org, 46695928494418.cgaux.org
    # These are clearly spam/phishing - note without creating rules for them
    # (they'd be better handled by spam filters)

    # ─── DATA BEES (INTERCOM) - feedback/surveys ──────────────────────────────
    (
        "feedback_data_bees",
        make_rule(
            "Feedback » Data Bees",
            "Feedback/Data Bees",
            from_domain("data-bees-f398bc68e0f6.intercom-mail.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Data Bees market research survey invitations via Intercom"]
        )
    ),

    # ─── WEEKDAY EMAIL SPAM (FAKE SAINSBURY'S) - spam ─────────────────────────
    # email.weekday.com appears to be spam impersonating Sainsbury's - skip

    # ─── JOBCOMFORTABLE (THURSDAY WORK) - newsletters ────────────────────────
    (
        "newsletters_marketing_thursday_work",
        make_rule(
            "Newsletters » Marketing » Thursday Work",
            "Newsletters/Marketing/Thursday Work",
            from_domain("jobcomfortable.com"),
            comments=["Created with IMAPFilter Rule Wizard",
                      "Thursday Work job listings newsletter (via jobcomfortable.com)"]
        )
    ),
]

# Write the files
written = 0
errors = []
ts_current = 1746400001

for (fname_suffix, rule_data) in rules:
    filename = f"{ts_current}_{fname_suffix}.json"
    filepath = os.path.join(RULES_DIR, filename)

    if os.path.exists(filepath):
        print(f"SKIP (exists): {filename}")
        ts_current += 1
        continue

    try:
        with open(filepath, 'w') as f:
            json.dump(rule_data, f, indent=2)
        print(f"WRITE: {filename}  →  {rule_data['actions'][0]['target']}")
        written += 1
    except Exception as e:
        errors.append(f"ERROR {filename}: {e}")
        print(errors[-1])

    ts_current += 1

print(f"\n✓ Written {written} rule files")
print(f"✗ Errors: {len(errors)}")
if errors:
    for e in errors:
        print(f"  {e}")

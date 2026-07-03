#!/usr/bin/env python3
"""
Fix priority conflicts in imapfilter-helper rules.
Classifies victim files as AUTO-FIX (priority → 90) or NEEDS-REVIEW.
"""

import json
import os
import sys

RULES_DIR = "/Users/stephenjgibson/imapfilter-helper/rules"

# ── All (victim_file, shadow_file, shadow_target, victim_target, reason_if_needs_review)
# reason_if_needs_review = None means AUTO-FIX
# Collected from analyze_rules.py output (211 conflicts)

CONFLICTS = [
    # [1]
    ("1777569869_domains_123_reg.json",
     "1745900001_server_123_reg.json",
     "Server/123 Reg", "Server/Domains/123 Reg", None),
    # [2]
    ("1777569869_kickstarter.json",
     "1745900002_newsletters_marketing_tech_backerpledge.json",
     "Newsletters/Marketing/Tech/Backerpledge", "Newsletters/Marketing/Retail/Kickstarter", None),
    # [3]
    ("1777569869_health_magdalen_health.json",
     "1745900006_health_appointments_cliniko.json",
     "Health/Appointments/Cliniko", "Personal/Health/Magdalen Health", None),
    # [4] & [5]
    ("1777569869_o2.json",
     "1745900012_notifications_o2.json",
     "Notifications/O2", "Subscriptions/O2", None),
    # [6] same victim
    ("1777569869_o2.json",
     "1745900013_newsletters_marketing_o2.json",
     "Newsletters/Marketing/O2", "Subscriptions/O2", None),
    # [7]
    ("1777569869_40th_birthday.json",
     "1745900014_receipts_restaurants_quayside_distillery.json",
     "Receipts/Restaurants/Quayside Distillery", "Events/40th Birthday", None),
    # [8-12]
    ("1777569869_21_cleveland_street_projects_windows.json",
     "1745900021_property_renovations_anglian.json",
     "Property/Renovations/Anglian", "21 Cleveland Street/Projects/Windows", None),
    # [13]
    ("1777569869_twitter.json",
     "1745900022_notifications_x.json",
     "Notifications/X", "Socials/Twitter", None),
    # [14] same victim as [7]
    ("1777569869_40th_birthday.json",
     "1746400011_receipts_events_the_hall_exeter.json",
     "Receipts/Events/The Hall Exeter", "Events/40th Birthday", None),
    # [15]
    ("261140756_newsletters_boston_tea_party.json",
     "1746300012_feedback_feeditback.json",
     "Feedback/FeedItBack", "Newsletters/Marketing/Restaurants/Boston Tea Party", None),
    # [16]
    ("1767641128_feedback_toolstation.json",
     "1746300021_newsletters_retail_toolstation.json",
     "Newsletters/Marketing/Retail/DIY/Toolstation", "Feedback/Toolstation", None),
    # [17]
    ("1777569869_toolstation.json",
     "1746300021_newsletters_retail_toolstation.json",
     "Newsletters/Marketing/Retail/DIY/Toolstation", "Notifications/Toolstation", None),
    # [18]
    ("1777569869_health_presecription_certificate.json",
     "1746400001_notifications_service_gov_uk.json",
     "Notifications/UK Government", "Personal/Health/Presecription Certificate", None),
    # [19]
    ("1777569869_lpa_jg.json",
     "1746400001_notifications_service_gov_uk.json",
     "Notifications/UK Government", "Personal/LPA/JG", None),
    # [20]
    ("1777569869_pensions_civil_service_pension.json",
     "1746400001_notifications_service_gov_uk.json",
     "Notifications/UK Government", "Personal/Finance/Pensions/Civil Service Pension", None),
    # [21]
    ("1777600007_health_coronavirus_testing.json",
     "1746400001_notifications_service_gov_uk.json",
     "Notifications/UK Government", "Health/Coronavirus Testing", None),
    # [22]
    ("253502075_travel_travel_alerts_gov_uk.json",
     "1746400001_notifications_service_gov_uk.json",
     "Notifications/UK Government", "Travel/Travel Alerts/GOV-UK", None),
    # [23]
    ("261140796_newsletters_parabola.json",
     "1746400002_newsletters_marketing_tech_parabola.json",
     "Newsletters/Marketing/Tech/Parabola", "Newsletters/Topics/Tech/Parabola", None),
    # [24] Xero invoicing → Archives/Rowing  (intentional specific address in broad rule)
    ("1767640859_archives_rowing.json",
     "1746400003_notifications_xero.json",
     "Notifications/Xero", "Archives/Rowing", None),
    # [25]
    ("1777569869_21_cleveland_street_projects_solar.json",
     "1746400007_property_renovations_des_renewables.json",
     "Property/Renovations/DES Renewables", "21 Cleveland Street/Projects/Solar", None),
    # [26]
    ("1777106913_notifications_wealthify.json",
     "1746400008_personal_finance_wealthify.json",
     "Personal/Finance/Wealthify", "Notifications/Wealthify", None),
    # [27] & [28]
    ("1777106914_newsletters_marketing_finance_wealthify.json",
     "1746400008_personal_finance_wealthify.json",
     "Personal/Finance/Wealthify", "Newsletters/Marketing/Finance/Wealthify", None),
    # [29] & [30]
    ("1777700030_personal_finance_investments_wealthify.json",
     "1746400008_personal_finance_wealthify.json",
     "Personal/Finance/Wealthify", "Personal/Finance/Investments/Wealthify", None),
    # [31]
    ("1777569869_recipets.json",
     "1746400009_subscriptions_noodlesoft_hazel.json",
     "Subscriptions/Noodlesoft Hazel", "Receipts", None),
    # [32] & [33]
    ("261140824_personal_pensions_aegon.json",
     "1746400010_personal_finance_aegon.json",
     "Personal/Finance/Aegon", "Personal/Finance/Pensions/Aegon", None),
    # [34] same victim as [7]
    ("1777569869_40th_birthday.json",
     "1746400011_receipts_events_the_hall_exeter.json",
     "Receipts/Events/The Hall Exeter", "Events/40th Birthday", None),
    # [35] same victim as [7]
    ("1777569869_40th_birthday.json",
     "1746400012_receipts_restaurants_cafe_mangos.json",
     "Receipts/Restaurants/Cafe Mangos", "Events/40th Birthday", None),
    # [36]
    ("1767641148_recipets_tesco.json",
     "1746400013_receipts_retail_grocery_tesco.json",
     "Receipts/Retail/Grocery/Tesco", "Receipts/Tesco", None),
    # [37]
    ("1767640924_jobs_search_microsoft_jobs.json",
     "1746400022_notifications_microsoft_onedrive.json",
     "Notifications/Microsoft", "Jobs/Search/Microsoft Jobs", None),
    # [38] & [39]
    ("1777569869_microsoft.json",
     "1746400022_notifications_microsoft_onedrive.json",
     "Notifications/Microsoft", "Receipts/Microsoft", None),
    # [40] & [41]
    ("1777569869_search_microsoft_jobs.json",
     "1746400022_notifications_microsoft_onedrive.json",
     "Notifications/Microsoft", "Jobs/Search/Microsoft Jobs", None),
    # [42] & [43]
    ("261140865_travel_flights_virgin_atlantic.json",
     "1746400024_travel_agents_virgin_holidays.json",
     "Travel/Agents/Virgin Holidays", "Travel/Flights/Virgin Atlantic", None),
    # [44]
    ("1777569869_21_cleveland_street_projects_heating.json",
     "1746400028_newsletters_marketing_mixergy.json",
     "Newsletters/Marketing/Mixergy", "21 Cleveland Street/Projects/Heating", None),
    # [45]
    ("261140783_newsletters_hume_health.json",
     "1746400037_health_services_hume_health.json",
     "Health/Services/Hume Health", "Newsletters/Marketing/Health/Hume Health", None),
    # [46]
    ("1777569869_travel_agents_loveholidays.json",
     "1746400040_feedback_delighted.json",
     "Feedback/Delighted", "Travel/Travel Agents/Loveholidays", None),
    # [47-54] — multiple uber conflicts, same victims
    ("1767641053_receipts_uber_eats.json",
     "1746400053_notifications_uber.json",
     "Notifications/Uber", "Receipts/Uber Eats", None),
    ("1767641113_newsletters_uber_eats.json",
     "1746400053_notifications_uber.json",
     "Notifications/Uber", "Newsletters/Marketing/Food/Uber Eats", None),
    ("1767641132_newsletters_uber.json",
     "1746400053_notifications_uber.json",
     "Notifications/Uber", "Newsletters/Marketing/Travel/Uber", None),
    ("253502047_travel_ground_transport_uber_train.json",
     "1746400053_notifications_uber.json",
     "Notifications/Uber", "Travel/Ground Transport/Uber Train", None),
    ("253502078_travel_ground_transport_uber.json",
     "1746400053_notifications_uber.json",
     "Notifications/Uber", "Travel/Ground Transport/Uber", None),
    # [61]
    ("1767640710_family.json",
     "1746400056_newsletters_marketing_new_scientist.json",
     "Newsletters/Marketing/New Scientist", "Family", None),
    # [62]
    ("261140826_21_cleveland_st_purchase.json",
     "1746400062_notifications_environment_agency.json",
     "Notifications/Environment Agency", "21 Cleveland Street/Purchase", None),
    # [63]
    ("261140744_fitness_domestiq.json",
     "1746400066_newsletters_marketing_domestiq.json",
     "Newsletters/Marketing/Domestiq", "Fitness/Domestiq", None),
    # [64]
    ("261140803_newsletters_the_brunswick.json",
     "1746400070_newsletters_marketing_the_brunswick.json",
     "Newsletters/Marketing/The Brunswick", "Newsletters/Marketing/Restaurants/The Brunswick", None),
    # [65]
    ("1777569869_wikipedia.json",
     "1746400087_charity_wikimedia.json",
     "Charity/Wikimedia", "Newsletters/Topics/Media/Wikipedia", None),
    # [66]
    ("1777700010_receipts_airalo.json",
     "1746400108_travel_airalo.json",
     "Travel/Airalo", "Receipts/Airalo", None),
    # [67-80] All exeter.ac.uk friends/work/alumni victims
    ("1767640283_friends.json",
     "1746400115_notifications_university_of_exeter.json",
     "Notifications/University of Exeter", "Friends", None),
    ("1777463016_notifications_exeter_university.json",
     "1746400115_notifications_university_of_exeter.json",
     "Notifications/University of Exeter", "Notifications/Exeter University", None),
    ("253470733_memberships_uoe_alumni.json",
     "1746400115_notifications_university_of_exeter.json",
     "Notifications/University of Exeter", "Memberships/UoE Alumni", None),
    ("253502045_jobs_current_uoe_exams.json",
     "1746400115_notifications_university_of_exeter.json",
     "Notifications/University of Exeter", "Jobs/Current/UoE Exams", None),
    # [90] & [91]
    ("1777569869_21_cleveland_street_projects_kitchen.json",
     "1767640863_newsletters_wren_kitchens.json",
     "Newsletters/Marketing/Retail/Home/Wren Kitchens", "21 Cleveland Street/Projects/Kitchen", None),
    # [92]
    ("1777569869_climbing_hanger.json",
     "1767640887_newsletters_fitness_the_climbing_hanger.json",
     "Newsletters/Marketing/Fitness/The Climbing Hanger", "Fitness/Climbing Hanger", None),
    # [93] & [94]
    ("253470730_jobs_search_linkedin_jobs.json",
     "1767640932_socials_linkedin.json",
     "Socials/Linkedin", "Jobs/Search/Linkedin Jobs", None),
    # [95] & [96]
    ("1767640944_notifications_ifttt.json",
     "1767640943_newsletters_ifttt.json",
     "Newsletters/Marketing/Tech/Software/IFTTT", "Notifications/IFTTT", None),
    # [97] Soulcycle: receipts shadow, events victim
    # Shadow: @mg.soul-cycle.com broad, Victim: noreply@mg.soul-cycle.com with subject "cancelled"
    # Shadow has ALL conditions including specific subject "SoulCycle Receipt: Order #*"
    # So only receipt-subject emails match shadow; victim is for cancel notifications -> AUTO-FIX
    ("1767640974_events_soulcycle.json",
     "1767640973_receipts_soulcycle.json",
     "Receipts/Soulcycle", "Events/Soulcycle", None),
    # [98]
    ("1767641124_personal_finance_store_credit_paypal.json",
     "1767640979_newsletters_paypal.json",
     "Newsletters/Marketing/Finance/PayPal", "Personal/Finance/Store Credit/Paypal", None),
    # [99] & [100]
    ("1777569869_21_cleveland_street_projects_heating.json",
     "1767641002_21_cleveland_street_bills_british_gas.json",
     "21 Cleveland Street/Bills/British Gas", "21 Cleveland Street/Projects/Heating", None),
    # [101]
    ("253470732_notifications_google_flights.json",
     "1767641004_notifications_google.json",
     "Notifications/Google", "Notifications/Google Flights", None),
    # [102] & [103] — icabbi SHADOW routes to Deleted Messages, victims route to taxi folders
    # Shadow is a deletion rule (pre-auth explanation, age>1 day, specific subject).
    # Victims catch ALL no-reply@icabbi.com. Shadow fires first only for that specific subject.
    # AUTO-FIX: victims are specific senders/displays while shadow catches broad domain + specific subject.
    ("1767641105_travel_ground_transport_apple_taxies.json",
     "1767641027_icabbie_pre_auth_explanation.json",
     "Deleted Messages", "Travel/Ground Transport/Apple Taxies", None),
    ("1777569869_ground_transport_a1_rushmoor_radio_taxis.json",
     "1767641027_icabbie_pre_auth_explanation.json",
     "Deleted Messages", "Travel/Ground Transport/A1 Rushmoor Radio Taxis", None),
    # [104]
    ("1777569869_dbrand.json",
     "1767641033_newsletters_dbrand.json",
     "Newsletters/Marketing/Tech/Hardware/dbrand", "Newsletters/Marketing/Tech/dbrand", None),
    # [105-108] Uber Eats newsletter vs receipts (noreply@uber.com / uber@uber.com)
    # Shadow: receipts_uber_eats catches noreply@/uber@uber.com; victim newsletter uses display name variants
    # AUTO-FIX: display-name patterns are more specific
    ("1767641113_newsletters_uber_eats.json",
     "1767641053_receipts_uber_eats.json",
     "Receipts/Uber Eats", "Newsletters/Marketing/Food/Uber Eats", None),
    # [109]
    ("253502047_travel_ground_transport_uber_train.json",
     "1767641053_receipts_uber_eats.json",
     "Receipts/Uber Eats", "Travel/Ground Transport/Uber Train", None),
    # [110]
    ("1767641074_support_apple.json",
     "1767641054_receipts_apple.json",
     "Receipts/Apple", "Support/Apple", None),
    # [111-115] Apple notification sub-types
    ("1767641126_notifications_apple_testflight.json",
     "1767641054_receipts_apple.json",
     "Receipts/Apple", "Notifications/Apple/Testflight", None),
    ("1767641133_notifications_apple_find_my.json",
     "1767641054_receipts_apple.json",
     "Receipts/Apple", "Notifications/Apple/Find My", None),
    ("1767641134_notifications_apple_icloud.json",
     "1767641054_receipts_apple.json",
     "Receipts/Apple", "Notifications/Apple/iCloud", None),
    ("1767641135_notifications_apple_subscriptions.json",
     "1767641054_receipts_apple.json",
     "Receipts/Apple", "Notifications/Apple/Subscriptions", None),
    # [116-118] Apple feedback
    ("1777569869_apple_feedback.json",
     "1767641054_receipts_apple.json",
     "Receipts/Apple", "Notifications/Apple/Feedback", None),
    # [119]
    ("1777569869_ikea_family.json",
     "1767641056_newsletters_ikea.json",
     "Newsletters/Marketing/Retail/Home/Ikea", "Newsletters/Marketing/Retail/Ikea Family", None),
    # [120] same victim as [31]
    ("1777569869_recipets.json",
     "1767641058_receipts_card_factory.json",
     "Receipts/Card Factory", "Receipts", None),
    # [121]
    ("1777569869_costa.json",
     "1767641079_feedback_costa_coffee.json",
     "Feedback/Costa Coffee", "Notifications/Costa", None),
    # [122] & [123]
    ("1767641098_notifications_whoop.json",
     "1767641097_newsletters_whoop.json",
     "Newsletters/Marketing/Fitness/Whoop", "Notifications/Whoop", None),
    # [124]
    ("1777569869_pensions_aegon_guild.json",
     "1767641115_newsletters_aegon.json",
     "Newsletters/Marketing/Finance/Aegon", "Personal/Finance/Pensions/Aegon Guild", None),
    # [125]
    ("253502047_travel_ground_transport_uber_train.json",
     "1767641132_newsletters_uber.json",
     "Newsletters/Marketing/Travel/Uber", "Travel/Ground Transport/Uber Train", None),
    # [126]
    ("1777569869_marriot_bonvoy.json",
     "1767641136_newsletters_marriot_bonvoy.json",
     "Newsletters/Marketing/Hotels/Marriott Bonvoy", "Newsletters/Marketing/Hotels/Marriot Bonvoy", None),
    # [127] & [128]
    ("261140815_notifications_hellofresh.json",
     "1767641139_newsletters_hellofresh.json",
     "Newsletters/Marketing/Food/HelloFresh", "Notifications/HelloFresh", None),
    # [129] & [130] AmEx SafeKey → Deleted Messages
    # Shadow rules ALREADY exclude SafeKey via not_contains, so victim's deletion is intentional.
    # AUTO-FIX: victim is more specific (specific address + specific subject + age_days_gt).
    ("1767641146_your_safekey_verification_code_deleted_messages.json",
     "1767641144_notifications_american_express.json",
     "Notifications/American Express", "Deleted Messages", None),
    ("1767641146_your_safekey_verification_code_deleted_messages.json",
     "1767641145_personal_finance_credit_cards_american_express.json",
     "Personal/Finance/Credit Cards/American Express", "Deleted Messages", None),
    # [131] & [132]
    ("1777569869_amtrak.json",
     "1767641173_travel_travel_loyalty_amtrak_guest_rewards.json",
     "Travel/Travel Loyalty/Amtrak Guest Rewards", "Newsletters/Marketing/Travel/Amtrak", None),
    # [133]
    ("261140839_receipts_gymshark.json",
     "1777106924_newsletters_marketing_fitness_gymshark.json",
     "Newsletters/Marketing/Fitness/Gymshark", "Receipts/Retail/Clothing/Gymshark", None),
    # [134-137]
    ("1777569869_asos.json",
     "1777106926_receipts_asos.json",
     "Receipts/ASOS", "Notifications/ASOS", None),
    # [138] & [139]
    ("253470730_jobs_search_linkedin_jobs.json",
     "1777412821_newsletters_linkedin.json",
     "Newsletters/Marketing/Work/LinkedIn", "Jobs/Search/Linkedin Jobs", None),
    # [140]
    ("253470653_the_economist.json",
     "1777412829_newsletters_economist.json",
     "Newsletters/Topics/Media/Economist", "The Economist", None),
    # [141]
    ("1777569869_finance_credit_cards_tsb.json",
     "1777412834_personal_banking_tsb.json",
     "Personal/Finance/Banking/TSB", "Personal/Finance/Credit Cards/TSB", None),
    # [142]
    ("1777569869_paperlesspost.json",
     "1777412837_notifications_paperless_post.json",
     "Notifications/Paperless Post", "Notifications/PaperlessPost", None),
    # [143]
    ("1777569869_finance_loans_v12_art_loan.json",
     "1777412838_personal_finance_store_credit_v12.json",
     "Personal/Finance/Store Credit/V12 Finance", "Personal/Finance/Loans/V12 Art Loan", None),
    # [144]
    ("1777569869_v12.json",
     "1777412838_personal_finance_store_credit_v12.json",
     "Personal/Finance/Store Credit/V12 Finance", "Newsletters/Marketing/Finance/V12", None),
    # [145-148]
    ("1777569869_21_cleveland_street_projects_windows.json",
     "1777412840_property_renovations_mps_windows.json",
     "21 Cleveland Street/Renovations/MPS Windows", "21 Cleveland Street/Projects/Windows", None),
    # [149] & [150]
    ("1777569869_finance_store_credit_verypay.json",
     "1777448205_personal_finance_store_credit_very.json",
     "Personal/Finance/Store Credit/Very", "Personal/Finance/Store Credit/Verypay", None),
    # [151] & [152]
    ("1777569869_finance_loans_santander.json",
     "1777448207_personal_banking_santander.json",
     "Personal/Finance/Banking/Santander", "Personal/Finance/Loans/Santander", None),
    # [153] & [154]
    ("1777569869_finance_trading_uphold.json",
     "1777448226_newsletters_marketing_finance_uphold.json",
     "Newsletters/Marketing/Finance/Uphold", "Personal/Finance/Trading/Uphold", None),
    # [155]
    ("1777569869_railcard.json",
     "1777448227_memberships_railcard.json",
     "Memberships/Railcard", "Newsletters/Marketing/Travel/Railcard", None),
    # [156] same victim as [31]
    ("1777569869_recipets.json",
     "1777448229_newsletters_marketing_travel_headout.json",
     "Newsletters/Marketing/Travel/Headout", "Receipts", None),
    # [157]
    ("1777569869_apple_feedback.json",
     "1777452200_feedback_apple_support.json",
     "Feedback/Apple Support", "Notifications/Apple/Feedback", None),
    # [158] & [159]
    ("1777569869_flights_easyjet.json",
     "1777452204_feedback_easyjet.json",
     "Feedback/easyJet", "Travel/Flights/EasyJet", None),
    # [160] & [161]
    ("1777569869_nzb_nzb_finder.json",
     "1777452208_newsletters_nzb_finder.json",
     "Newsletters/Marketing/Tech/NZB Finder", "Server/NZB/NZB Finder", None),
    # [162]
    ("1777569869_ground_transport_freenow.json",
     "1777452220_travel_ground_transport_freenow.json",
     "Travel/Ground Transport/FreeNow", "Travel/Ground Transport/Freenow", None),
    # [163] BuddyBoost: Fitness (broad @buddyboost.co.uk) vs Newsletters (specific donotreply@)
    # Victim is more specific, AUTO-FIX
    ("1777569869_buddyboost.json",
     "1777452227_fitness_buddyboost.json",
     "Fitness/BuddyBoost", "Newsletters/Marketing/Fitness/Buddyboost", None),
    # [164] & [165] Cedar Tree Insurance: shadow→Personal/Finance/Insurance, victim→Travel/Insurance
    # Victim routes to Travel/Insurance which is a valid category for travel insurance
    # This is AUTO-FIX (specific address vs domain)
    ("1777569869_insurance.json",
     "1777452229_personal_finance_insurance_cedar_tree.json",
     "Personal/Finance/Insurance/Cedar Tree", "Travel/Insurance", None),
    # [166-168]
    ("1777569869_purchase_21_cleveland_st_survey.json",
     "1777453315_21_cleveland_street_purchase_surveyors_steren.json",
     "21 Cleveland Street/Purchase/Surveyors", "Property/Purchase/21 Cleveland St/Survey", None),
    # [169] same victim as [31]
    ("1777569869_recipets.json",
     "1777453321_receipts_restaurants_candlemaker.json",
     "Receipts/Restaurants/Candlemaker", "Receipts", None),
    # [170]
    ("1777569869_burger_king.json",
     "1777453324_receipts_restaurants_burger_king.json",
     "Receipts/Restaurants/Burger King", "Receipts/Burger King", None),
    # [171]
    ("1777569869_vue.json",
     "1777453333_receipts_entertainment_vue.json",
     "Receipts/Entertainment/Vue", "Tickets/Vue", None),
    # [172] & [173]
    ("1777569869_travel_agents_loveholidays.json",
     "1777453340_travel_bookings_loveholidays.json",
     "Travel/Bookings/Loveholidays", "Travel/Travel Agents/Loveholidays", None),
    # [174]
    ("1777569869_flights_delta.json",
     "1777453342_receipts_travel_delta.json",
     "Receipts/Travel/Delta", "Travel/Flights/Delta", None),
    # [175] & [176]
    ("261140761_newsletters_comic_con.json",
     "1777463001_memberships_comic_con.json",
     "Memberships/Comic-Con", "Newsletters/Marketing/Entertainment/Comic Con", None),
    # [177]
    ("261140799_newsletters_rock_the_boat.json",
     "1777463004_newsletters_outdoors_rock_the_boat.json",
     "Newsletters/Marketing/Outdoors/Rock the Boat", "Newsletters/Marketing/Entertainment/Rock The Boat", None),
    # [178] & [179]
    ("1777569869_21_cleveland_street_projects_solar.json",
     "1777463006_notifications_glow_green.json",
     "Notifications/Glow Green", "21 Cleveland Street/Projects/Solar", None),
    # [180]
    ("261140741_21_cleveland_street_renovations.json",
     "1777463012_notifications_quickbooks.json",
     "Notifications/QuickBooks", "21 Cleveland Street/Projects", None),
    # [181]
    ("261140742_events_venuescanner.json",
     "1777463013_notifications_venue_scanner.json",
     "Notifications/Venue Scanner", "Events/VenueScanner", None),
    # [182]
    ("1777569869_21_cleveland_street_projects_solar.json",
     "1777463014_notifications_solar_guide.json",
     "Notifications/Solar Guide", "21 Cleveland Street/Projects/Solar", None),
    # [183]
    ("261140805_newsletters_trainpal.json",
     "1777463015_travel_ground_transport_mytrainpal.json",
     "Travel/Ground Transport/MyTrainPal", "Newsletters/Marketing/Travel/Trainpal", None),
    # [184]
    ("261140773_newsletters_grafter.json",
     "1777463017_receipts_grafterr.json",
     "Receipts/Grafterr", "Newsletters/Marketing/Work/Grafter", None),
    # [185]
    ("1777569869_software_keys.json",
     "1777500001_receipts_paddle.json",
     "Receipts/Paddle", "Software Keys", None),
    # [186]
    ("1777569869_flights_flybe.json",
     "1777500002_travel_flights_flybe.json",
     "Travel/Flights/Flybe", "Travel/Flights/FlyBe", None),
    # [187]
    ("1777569869_gusto.json",
     "1777500006_newsletters_marketing_food_gousto.json",
     "Newsletters/Marketing/Food/Gousto", "Subscriptions/Gusto", None),
    # [188]
    ("1777569869_finance_trading_trading212.json",
     "1777500010_personal_finance_investments_trading212.json",
     "Personal/Finance/Investments/Trading212", "Personal/Finance/Trading/Trading212", None),
    # [189] & [190] & [191] same victim as [31]
    ("1777569869_recipets.json",
     "1777500018_receipts_square.json",
     "Receipts/Square", "Receipts", None),
    ("1777569869_recipets.json",
     "1777500024_receipts_hutch_house_plants.json",
     "Receipts/Hutch House Plants", "Receipts", None),
    # [192] Met Office catches broad @metoffice.gov.uk; victim is specific sender+subject (CloudNine gift vouchers)
    ("253502042_gift_vouchers_cloudnine.json",
     "1777569869_current_met_office.json",
     "Jobs/Current/Met Office", "Gift Vouchers/CloudNine", None),
    # [193-195] Hollister misspelled shadow catches broad hollisterco.com; victims are specific subdomain patterns
    # AUTO-FIX
    ("253470710_newsletters_hollister.json",
     "1777569869_holister.json",
     "Newsletters/Marketing/Retail/Holister", "Newsletters/Marketing/Retail/Clothing/Hollister", None),
    ("261140779_newsletters_hollister.json",
     "1777569869_holister.json",
     "Newsletters/Marketing/Retail/Holister", "Newsletters/Marketing/Retail/Clothing/Hollister", None),
    # [196] & [197]
    ("253470702_newsletters_productivity_flourising.json",
     "1777569869_substack.json",
     "Substack", "Newsletters/Topics/Learning/Productive Flourishing", None),
    # [198]
    ("253502055_receipts_airvpn.json",
     "1777600002_receipts_stripe.json",
     "Receipts/Stripe", "Receipts/AirVPN", None),
    # [199]
    ("261140791_newsletters_made_tech.json",
     "1777600008_newsletters_work_madetech.json",
     "Newsletters/Marketing/Work/Made Tech", "Newsletters/Topics/Tech/Made Tech", None),
    # [200]
    ("261140816_notifications_peloton.json",
     "1777600014_fitness_peloton.json",
     "Fitness/Peloton", "Notifications/Peloton", None),
    # [201]
    ("261140856_travel_flights_general.json",
     "1777700020_travel_travel_agents_major_travel.json",
     "Travel/Travel Agents/Major Travel", "Travel/Flights/General", None),
    # [202] & [203]
    ("261140875_travel_agents_omega_flightstore.json",
     "1777700022_travel_travel_agents_omega_travel.json",
     "Travel/Travel Agents/Omega Travel", "Travel/Travel Agents/Omega Flightstore", None),
    # [204]
    ("261140738_21_cleveland_street_projects.json",
     "1777800011_property_renovations_environmentuk.json",
     "Property/Renovations", "21 Cleveland Street/Projects", None),
    # [205] Howabout: shadow _at_howbout_app_ catches contact_at_howbout_app_ (substring match)
    # Both are iCloud private relay addresses. AUTO-FIX: victim has more specific prefix.
    ("253502028_notifications_howbout_app.json",
     "1777800019_newsletters_howabout.json",
     "Newsletters/Howabout", "Notifications/Howbout", None),
    # [206] & [207]
    ("253470682_personal_finance_credit_rating_clearscore.json",
     "253470681_newsletters_clearscore.json",
     "Newsletters/Marketing/Finance/Clearscore", "Personal/Finance/Credit Rating/ClearScore", None),
    # [208]
    ("253470769_newsletters_south_west_water.json",
     "253470768_21_cleveland_street_bills_southwest_water.json",
     "21 Cleveland Street/Bills/Southwest Water", "Newsletters/Marketing/Retail/South West Water", None),
    # [209]
    ("253502044_newsletters_openrent.json",
     "253470781_21_cleveland_street_lodger_adverts_openrent.json",
     "21 Cleveland Street/Lodger/Adverts/OpenRent", "Newsletters/Marketing/Property/OpenRent", None),
    # [210]
    ("253470783_newsletters_spareroonm.json",
     "253470782_21_cleveland_street_lodger_adverts_spareroom.json",
     "21 Cleveland Street/Lodger/Adverts/Spareroom", "Newsletters/Marketing/Property/SpareRoonm", None),
    # [211]
    ("253502067_property_zoopla.json",
     "253502066_newsletters_zoopla.json",
     "Newsletters/Marketing/Property/Zoopla", "Property/Zoopla", None),
]


def main():
    rules_dir = RULES_DIR

    # Collect unique victims and their NEEDS-REVIEW status
    victim_data = {}  # filename -> {"shadow_targets": [], "victim_target": str, "needs_review": str|None}

    for victim_file, shadow_file, shadow_target, victim_target, needs_review in CONFLICTS:
        if victim_file not in victim_data:
            victim_data[victim_file] = {
                "shadow_targets": [],
                "victim_target": victim_target,
                "needs_review": needs_review,
            }
        victim_data[victim_file]["shadow_targets"].append((shadow_file, shadow_target))
        # If any conflict flags needs_review, mark it
        if needs_review is not None:
            victim_data[victim_file]["needs_review"] = needs_review

    auto_fix_count = 0
    already_not_100 = []
    needs_review_cases = []
    auto_fix_files = []

    for victim_file, info in victim_data.items():
        fpath = os.path.join(rules_dir, victim_file)
        if not os.path.exists(fpath):
            print(f"  WARNING: File not found: {victim_file}", file=sys.stderr)
            continue

        with open(fpath) as f:
            rule = json.load(f)

        current_priority = rule.get("priority")

        if info["needs_review"] is not None:
            needs_review_cases.append({
                "victim_file": victim_file,
                "shadow_files": [s for s, _ in info["shadow_targets"]],
                "shadow_targets": [t for _, t in info["shadow_targets"]],
                "victim_target": info["victim_target"],
                "reason": info["needs_review"],
            })
            continue

        if current_priority != 100:
            already_not_100.append((victim_file, current_priority))
            continue

        # AUTO-FIX: change priority to 90
        rule["priority"] = 90
        with open(fpath, "w") as f:
            json.dump(rule, f, indent=2, ensure_ascii=False)
            f.write("\n")

        auto_fix_count += 1
        auto_fix_files.append(victim_file)

    # ── Report ─────────────────────────────────────────────────────────────────

    print(f"\n{'='*72}")
    print(f"  PRIORITY CONFLICT FIX REPORT")
    print(f"{'='*72}")
    print(f"\n  AUTO-FIX: {auto_fix_count} rule file(s) updated (priority 100 → 90)\n")
    for f in sorted(auto_fix_files):
        print(f"    ✓ {f}")

    print(f"\n{'='*72}")
    print(f"  NEEDS-REVIEW: {len(needs_review_cases)} case(s)\n")
    if needs_review_cases:
        for c in needs_review_cases:
            shadow_str = ", ".join(set(c["shadow_files"]))
            shadow_target_str = ", ".join(set(c["shadow_targets"]))
            print(f"  Victim  : {c['victim_file']}")
            print(f"  Shadow  : {shadow_str}")
            print(f"  Shadow→ : {shadow_target_str}")
            print(f"  Victim→ : {c['victim_target']}")
            print(f"  Reason  : {c['reason']}")
            print()

    if already_not_100:
        print(f"{'='*72}")
        print(f"  SKIPPED (already not at priority 100): {len(already_not_100)}\n")
        for f, p in already_not_100:
            print(f"    • {f}  (priority={p})")

    print(f"\n{'='*72}\n")


if __name__ == "__main__":
    main()

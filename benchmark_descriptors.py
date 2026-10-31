"""benchmark_descriptors.py — real-shaped statement strings with true labels.

Descriptors are written the way banks actually emit them: processor prefixes,
store numbers, cities, phone numbers, auth codes, and issuer-specific preambles.
Used to measure categorisation accuracy, not to demonstrate it.
"""

# (descriptor, true_category)
CASES = [
    # --- payment processor prefixes: the real merchant hides behind them ---
    ("SQ *BLUE BOTTLE COFFEE OAKLAND CA", "Coffee"),
    ("TST* JOE'S PIZZA - BLEECKER NEW YORK NY", "Dining"),
    ("PP*GRUBHUB FOOD 4029357733 NY", "Delivery"),
    ("PAYPAL *STEAMGAMES 4029357733 WA", "Gaming"),
    ("SP AFFIRM CLOTHING SHOP HTTPSAFFIRM. CA", "Clothing"),
    ("IC* INSTACART SAN FRANCISCO CA", "Groceries"),
    ("WPY*CROSSFIT MIDTOWN 855-469-2909 CA", "Fitness"),
    ("EB *BROOKLYN STEEL SHOW 8014135200 CA", "Entertainment"),
    ("SQ *THE BARBER ROOM BROOKLYN NY", "Personal Care"),
    ("TST*SWEETGREEN - FLATIRON NEW YORK NY", "Dining"),

    # --- issuer preambles that bury the merchant mid-string ---
    ("PURCHASE AUTHORIZED ON 03/12 TRADER JOE'S #542 NEW YORK NY S465071234567 CARD 1234", "Groceries"),
    ("CHECKCARD 0312 WHOLE FOODS MKT 10289 NEW YORK NY 24492161234", "Groceries"),
    ("POS DEBIT SHELL OIL 57444789002 BAYONNE NJ", "Gas/Auto"),
    ("RECURRING PAYMENT AUTHORIZED ON 04/03 NETFLIX.COM 866-579-7172 CA", "Subscriptions"),
    ("DEBIT CARD PURCHASE CVS/PHARMACY #04412 JERSEY CITY NJ", "Pharmacy"),
    ("PURCHASE AUTHORIZED ON 05/22 UBER TRIP HELP.UBER.COM CA S305142345678", "Transport"),
    ("ACH DEBIT CONED BILL PAY 800-752-6633 NY", "Utilities"),

    # --- amazon's many faces ---
    ("AMZN Mktp US*RT4G92H1 AMZN.COM/BILL WA", "Shopping"),
    ("AMAZON.COM*8N2KL0R91 SEATTLE WA", "Shopping"),
    ("Amazon Prime*2H8KL4M22 888-802-3080 WA", "Subscriptions"),
    ("AMZN DIGITAL*4K2L9 888-802-3080 WA", "Subscriptions"),
    ("WHOLEFDS MKT 10289 NEW YORK NY", "Groceries"),

    # --- transport that isn't uber-the-ride ---
    ("UBER EATS 8005928996 CA", "Delivery"),
    ("UBER *TRIP HELP.UBER.COM CA", "Transport"),
    ("LYFT *RIDE SAT 5PM SAN FRANCISCO CA", "Transport"),
    ("MTA*NYCT PAYGO NEW YORK NY", "Transport"),
    ("NJ TRANSIT MOBILE NEWARK NJ", "Transport"),
    ("PORT AUTHORITY PATH NEW YORK NY", "Transport"),
    ("E-ZPASS REBILL 888-288-6865 NJ", "Transport"),
    ("SPOTHERO PARKING 888-807-0209 IL", "Transport"),

    # --- the ambiguous ones a naive regex gets wrong ---
    ("MARKETPLACE INSURANCE PREMIUM 800-318-2596", "Insurance"),
    ("STOCK MARKET NEWS SUBSCRIPTION", "Subscriptions"),
    ("PETROLEUM SERVICES OF NJ", "Gas/Auto"),
    ("PET SUPPLIES PLUS #221 HOBOKEN NJ", "Pets"),
    ("INN AT PERRY CABIN ST MICHAELS MD", "Lodging"),
    ("BEGINNING BALANCE ADJUSTMENT", "Uncategorized"),
    ("THE HOME DEPOT #1234 JERSEY CITY NJ", "Home"),
    ("HOMEGOODS #0412 SECAUCUS NJ", "Home"),
    ("GAP OUTLET #6612 ELIZABETH NJ", "Clothing"),
    ("GAPCO ONLINE 800-427-7895 CA", "Clothing"),

    # --- subscriptions and tools ---
    ("OPENAI *CHATGPT SUBSCR SAN FRANCISCO CA", "Software/Tools"),
    ("ANTHROPIC CLAUDE PRO SAN FRANCISCO CA", "Software/Tools"),
    ("GITHUB*COPILOT HTTPSGITHUB.C CA", "Software/Tools"),
    ("ADOBE *CREATIVE CLOUD 408-536-6000 CA", "Software/Tools"),
    ("JETBRAINS AMERICAS INC 617-3861234 MA", "Software/Tools"),
    ("AWS EMEA amazon.co LUXEMBOURG", "Software/Tools"),
    ("GOOGLE *CLOUD 6E7A2B cc@google.com CA", "Software/Tools"),
    ("APPLE.COM/BILL 866-712-7753 CA", "Subscriptions"),
    ("ICLOUD STORAGE 50GB 866-712-7753 CA", "Subscriptions"),
    ("SPOTIFY USA 877-7781161 NY", "Subscriptions"),
    ("HULU 888-2657737 CA", "Subscriptions"),
    ("DISNEY PLUS 888-905-7888 CA", "Subscriptions"),
    ("NYTimes*NYTimes 800-698-4637 NY", "Subscriptions"),
    ("PATREON* MEMBERSHIP 855-5064555 CA", "Subscriptions"),

    # --- money in, which must never read as spending ---
    ("PLAYCETERA INC ACH PMT PPD ID: 1234567890", "Uncategorized"),
    ("ACME CORP PAYROLL DIRECT DEP PPD ID: 9876543210", "Income"),
    ("IRS TREAS 310 TAX REF PPD ID: 3387702000", "Income"),
    ("ZELLE FROM SARAH MILLER ON 04/12", "Income"),
    ("VENMO CASHOUT 855-812-4430 NY", "Transfers/Cash"),
    ("VENMO PAYMENT 855-812-4430 NY", "Transfers/Cash"),
    ("INTEREST PAID THIS STATEMENT PERIOD", "Income"),
    ("MOBILE DEPOSIT REF 4429183", "Income"),

    # --- payments and fees ---
    ("PAYMENT - THANK YOU", "Payment"),
    ("AUTOPAY PAYMENT THANK YOU", "Payment"),
    ("ONLINE PAYMENT FROM CHK 1234", "Payment"),
    ("INTEREST CHARGE ON PURCHASES", "Fees/Interest"),
    ("ANNUAL MEMBERSHIP FEE", "Fees/Interest"),
    ("FOREIGN TRANSACTION FEE", "Fees/Interest"),
    ("LATE FEE - ASSESSED 04/22", "Fees/Interest"),
    ("CASH ADVANCE FEE 5.00", "Fees/Interest"),

    # --- debt that looks like a bill ---
    ("AFFIRM PAY XKCD9J2 855-423-3729 CA", "Debt Payment"),
    ("KLARNA*INSTALLMENT 844-552-7621", "Debt Payment"),
    ("NAVIENT STUDENT LOAN PMT PPD", "Debt Payment"),
    ("CHASE AUTO LOAN PAYMENT", "Debt Payment"),

    # --- groceries vs big box vs pharmacy, which overlap constantly ---
    ("TARGET 00023881 JERSEY CITY NJ", "Big Box"),
    ("TARGET.COM * 800-591-3869 MN", "Big Box"),
    ("WAL-MART SUPERCENTER #2648 SECAUCUS NJ", "Big Box"),
    ("COSTCO WHSE #0331 BROOKLYN NY", "Big Box"),
    ("COSTCO GAS #0331 BROOKLYN NY", "Gas/Auto"),
    ("WALGREENS #4412 JERSEY CITY NJ", "Pharmacy"),
    ("SHOPRITE OF JERSEY CITY NJ", "Groceries"),
    ("KEY FOOD #1122 BROOKLYN NY", "Groceries"),
    ("H MART JERSEY CITY NJ", "Groceries"),
    ("WESTSIDE MARKET NYC NEW YORK NY", "Groceries"),

    # --- dining shapes ---
    ("THE HALAL GUYS 53RD NEW YORK NY", "Dining"),
    ("CHIPOTLE 1892 JERSEY CITY NJ", "Dining"),
    ("SHAKE SHACK 0142 NEW YORK NY", "Dining"),
    ("DUNKIN #338910 JERSEY CITY NJ", "Coffee"),
    ("STARBUCKS STORE 09112 JERSEY CITY NJ", "Coffee"),
    ("PHILZ COFFEE #221 SAN FRANCISCO CA", "Coffee"),
    ("DOORDASH*CHIPOTLE 855-431-0459 CA", "Delivery"),
    ("SEAMLESS*JOES PIZZA NEW YORK NY", "Delivery"),
    ("THE DEAD RABBIT NEW YORK NY", "Alcohol/Bars"),
    ("BROOKLYN BREWERY BROOKLYN NY", "Alcohol/Bars"),
    ("TOTAL WINE & MORE #1234 NJ", "Alcohol/Bars"),

    # --- travel ---
    ("DELTA AIR LINES 0062318842211 ATLANTA GA", "Travel"),
    ("JETBLUE AIRWAYS 2792101234567 NY", "Travel"),
    ("MARRIOTT BONVOY MIAMI FL", "Lodging"),
    ("AIRBNB * HMQ2K3J4 AIRBNB.COM CA", "Lodging"),
    ("HERTZ RENT-A-CAR NEWARK NJ", "Travel"),
    ("TSA PRECHECK APPLICATION", "Travel"),

    # --- health, which must never be roasted or mislabelled ---
    ("ONE MEDICAL MEMBERSHIP 415-291-0480 CA", "Health"),
    ("QUEST DIAGNOSTICS 866-697-8378 NJ", "Health"),
    ("CITYMD URGENT CARE NEW YORK NY", "Health"),
    ("DR SARAH CHEN DDS JERSEY CITY NJ", "Health"),
    ("GOODRX GOLD 855-268-2822 CA", "Pharmacy"),

    # --- utilities and housing ---
    ("PSE&G ELECTRIC BILLPAY 800-436-7734 NJ", "Utilities"),
    ("VERIZON WIRELESS 800-922-0204 NJ", "Phone/Internet"),
    ("T-MOBILE POSTPAID 800-937-8997 WA", "Phone/Internet"),
    ("OPTIMUM ONLINE 973-230-6048 NJ", "Phone/Internet"),
    ("MERIDIAN PROPERTY MGMT RENT 04/01", "Rent/Housing"),
    ("PUBLIC STORAGE 24601 800-567-0759 CA", "Home"),

    # --- education, charity, gifts ---
    ("COURSERA *SPECIALIZATION 650-620-9915 CA", "Education"),
    ("NYU BURSAR TUITION 212-998-2806 NY", "Education"),
    ("GOFUNDME* HELP FOR ANNA 855-816-4483 CA", "Charity"),
    ("RED CROSS DONATION 800-435-7669 DC", "Charity"),
    ("1-800-FLOWERS.COM 800-356-9377 NY", "Gifts"),

    # --- genuinely unknowable without asking ---
    ("SKYVAULT DIGITAL LLC 8885551212", "Uncategorized"),
    ("NORTHSTAR HOLDINGS LLC", "Uncategorized"),
    ("TRANSACTION 4429183 REF 88213", "Uncategorized"),

    # ----------------------------------------------------------------------
    # Deposit-account shapes. Everything above is a card statement, and card
    # statements are the easy half: a bank statement names its own transaction
    # type in the descriptor, then buries the merchant behind a posting date.
    # These were the cases that left 60% of real spending uncategorised.
    # ----------------------------------------------------------------------

    # Chase posting-date preambles, which hide the merchant mid-string
    ("Recurring Card Purchase 01/01 Digitalocean.Com Digitalocean. NY Card", "Software/Tools"),
    ("Card Purchase With Pin 02/13 Target T-1027 Dayton OH Card 1234", "Big Box"),
    ("Card Purchase Return 03/20 Amazon Mktplace Pmts Amzn.Com/Bill WA", "Credits/Refunds"),
    ("Recurring Card Purchase 04/17 Name-Cheap.Com* Misq Www.Namecheap", "Software/Tools"),
    ("AplPay DOMINO'S 3384 000003384 JERSEY CITY NJ", "Dining"),

    # Money between accounts you own is a transfer; money from someone else is
    # income. Both are non-spend, but calling them the same thing is a lie.
    ("Zelle Payment From Jordan Rivera Xk4Q8Mz2Pa71", "Income"),
    ("Zelle Payment To Alex Chen Jpm42B81Rr9K", "Transfers/Cash"),
    ("ATM Cash Deposit 12/26 120 Main St Dayton OH Card 1234", "Transfers/Cash"),
    ("ATM Withdrawal 01/24 120 Main St Dayton OH Card 1234", "Transfers/Cash"),
    ("Online Transfer From Sav ...4321 Transaction#: 12345678901", "Transfers/Cash"),
    ("Book Transfer Credit B/O: National Financial Services LLC Smithfield RI",
     "Transfers/Cash"),
    ("Fid Bkg Svc LLC Moneyline Z000123456A12Bc Web ID: 1000000001", "Transfers/Cash"),
    ("Real Time Payment Credit Recd From Aba/Contr Bnk-011000015 From:", "Income"),
    ("Amex Send: Add Money", "Transfers/Cash"),
    ("4410 Northwind Cafe Payroll PPD ID: 1234509876", "Income"),
    ("State of Ohio Ohtaxrfd PPD ID: E000000001", "Income"),

    # Card payments made from checking. Real outflows from the bank account,
    # but the same money already appears as purchases on the card statement,
    # so counting them as spending double-counts every dollar.
    ("American Express ACH Pmt M1234 Web ID: 2000000001", "Payment"),
    ("Discover E-Payment 1234 Web ID: 3500000001", "Payment"),
    ("Payment To Chase Pay IN 4 (...1111)", "Payment"),
    ("Pay IN 4 Deposit - POS Debit Anthropic* (...2222)", "Software/Tools"),

    # Names glued to their city and state with no separator at all
    ("BIG Y 46 WORCESTERWORCESTERMA", "Groceries"),
    ("CUMBERLAND FARMS 6617WORCESTERMA", "Gas/Auto"),
    ("CinemarkDaytonOH", "Entertainment"),
    ("DOLLAR TREEDAYTONOH", "Shopping"),
    ("JCPENNEY 2022DAYTONOH", "Shopping"),
    ("833-632-2778 ELECTRIFYHUNT VALLEYMD", "Gas/Auto"),
    ("TST* SHERPA HIMALAYAN BDENVERCO", "Dining"),
    ("RPS*Lakeview Apar RDDaytonOH", "Rent/Housing"),
    ("Pf Dayton OH Iclub Fees PPD ID: G000000001", "Fitness"),

    # Capital One and Discover print their own MCC label on the end of the
    # line. It is a useful last resort, but it must lose to a keyword match:
    # a liquor store tagged "Merchandise" is still Alcohol/Bars.
    ("VALLEY LIVE POULTRY DAYTON OH Supermarkets", "Groceries"),
    ("SHELL10012345678 DAYTON OH Gasoline", "Gas/Auto"),
    ("ZIPCAR TRIP CHICAGO IL Travel/Entertainment", "Travel"),

    ("WWW.PERPLEXITY.AI SAN FRANCISCO CA", "Software/Tools"),
    ("ERACTOLL 1ABCDE 877-860-1258 OH", "Transport"),

    # "rent" is the one housing word that usually is not housing. Filing a car
    # hire as Rent/Housing inflates essential spending, which is what the
    # emergency-fund target is sized from.
    ("ENTERPRISE RENT-A-CAR NOR", "Travel"),
    ("AVIS RENT A CAR PHX AZ", "Travel"),
    ("RENTAL CAR CENTER SFO", "Travel"),
    ("GREYSTAR RENT PAYMENT", "Rent/Housing"),
    ("APARTMENT LEASE PMT 04/01", "Rent/Housing"),
]

if __name__ == "__main__":
    from collections import Counter
    print(f"{len(CASES)} labelled descriptors")
    for c, n in Counter(cat for _, cat in CASES).most_common():
        print(f"  {c:<18}{n:>3}")
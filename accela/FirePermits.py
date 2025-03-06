import requests
import json
import os

os.system('cls')

URL = "https://apis.accela.com/v4/records"
AUTH_URL = "https://auth.accela.com/oauth2/token"

# Input variables from FireAside
DATE_PERMIT_ISSUED = "2/18/2025"
LODGEPOLE_PINE = 2
INCENSE_CEDAR = 3
JEFFREY_PINE = 4
WHITE_FIR = 5
RED_FIR = 3
PONDEROSA_PINE = 1
OTHER_TREE = 2
SPECIAL_CONDITIONS = "Enter special conditions here" 
PROPERTY_ACCESS_INFO = "Enter property access info"
APN = "027-114-021"
FIRST_NAME = "Amy"
LAST_NAME = "Fish"
FULL_NAME = "Amy Fish"
PHONE = "775-589-5273"
EMAIL = "afish@trpa.gov"
ORGANIZATION_NAME = "Enter Org"
DOC_DESCRIPTION = "Tahoe Basin Tree Removal Permit PDF"
DOCUMENT_PATH = "c:/temp/TreePermitTest.pdf"

# Authentication Credentials
CLIENT_ID = "637946576467549438"
CLIENT_SECRET = "b6523f19a56047f8a1b8220c146b9178"
GRANT_TYPE = "password"
USERNAME = "FIREASIDE"
PASSWORD = "123456FA!"
AGENCY_NAME = "TRPA"
ENVIRONMENT = "NONPROD1"
SCOPE = "records documents parcels"

# Authorize
AUTH_PAYLOAD = {
    "client_id": CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "grant_type": GRANT_TYPE,
    "username": USERNAME,
    "password": PASSWORD,
    "agency_name": AGENCY_NAME,
    "environment": ENVIRONMENT,
    "scope": SCOPE
}
AUTH_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded"
}

# Make the POST request to get the access token
AUTH_RESPONSE = requests.post(AUTH_URL, data=AUTH_PAYLOAD, headers=AUTH_HEADERS)

if AUTH_RESPONSE.status_code == 200:
    ACCESS_TOKEN = AUTH_RESPONSE.json().get("access_token")
    print("Access Token:", ACCESS_TOKEN)
else:
    print("Error:", AUTH_RESPONSE.status_code, AUTH_RESPONSE.text)
    exit()  # Exit if token generation fails

# Get the address record for the parcel
URL = f"https://apis.accela.com/v4/parcels/{APN}/addresses"

HEADERS = {
    "Authorization": f"{ACCESS_TOKEN}",
    "Content-Type": "application/json"
}
RESPONSE = requests.request("GET", URL, headers=HEADERS)
if RESPONSE.status_code == 200:
    print(RESPONSE.text)
else:
    print("Address not found")

input_json = json.loads(RESPONSE.text)

output_json = {
    "addresses": [
        {
            "id": record["id"],
            "keyFieldsNull": False,
            "refAddressId": record["id"],
            "streetStart": record["streetStart"],
            "city": record["city"],
            "streetName": record["streetName"],
            "postalCode": record["postalCode"],
            "isPrimary": record["isPrimary"],
            "serviceProviderCode": "TRPA",
            "state": {
                "value": record["state"]["value"],
                "text": record["state"]["text"]
            },
            "streetSuffix": {
                "value": record["streetSuffix"]["value"],
                "text": record["streetSuffix"]["text"]
            }
        }
        for record in input_json["result"]
    ]
}

URL = "https://apis.accela.com/v4/records"

PAYLOAD = json.dumps({
    "customForms": [
        {
            "id": "B_TREE_RMVL-GENERAL",
            "Level of Review": "Staff Level",
            "G-Code": "049 - Tree Removal",
            "Date Permit Issued": DATE_PERMIT_ISSUED
        },
        {
            "id": "B_TREE_RMVL-TREES TO BE REMOVED",
            "Lodgepole Pine": str(LODGEPOLE_PINE),
            "Incense Cedar": str(INCENSE_CEDAR),
            "Jeffrey Pine": str(JEFFREY_PINE),
            "Ponderosa Pine": str(PONDEROSA_PINE),
            "White Fir": str(WHITE_FIR),
            "Red Fir": str(RED_FIR),
            "Other": str(OTHER_TREE),
            "Tree Total": str(OTHER_TREE + LODGEPOLE_PINE + INCENSE_CEDAR + JEFFREY_PINE + WHITE_FIR + RED_FIR + PONDEROSA_PINE),
            "Special Conditions": SPECIAL_CONDITIONS
        },
        {
            "Defensible Space": "CHECKED",
            "id": "B_TREE_RMVL-REASON.cFOR.cREMOVAL"
        },
        {
            "Property Access Information": PROPERTY_ACCESS_INFO,
            "id": "B_TREE_RMVL-PROJECT.cDESCRIPTION"
        }
    ],
    "customId": None,
    "description": "Created by FireAside",
    "id": None,
    "openedDate": "2024-06-17 10:38:35",
    "parcels": [
        {
            "id": APN,
            "parcelNumber": APN
        }
    ],
    "status": {
        "text": "Approved",
        "value": "Approved"
    },
    "assignedUser": "BBARR",
    "assignedToDepartment": "TRPA/ERS/NA/NA/NA/FORESTRY/NA",
    "type": {
        "module": "Building",
        "value": "Building/ERS/Permits/Tree Removal",
        "type": "ERS",
        "text": "Tree Removal",
        "group": "Building",
        "alias": "Tree Removal",
        "category": "Tree Removal",
        "subType": "Permits",
        "id": "Building-ERS-Permits-Tree.cRemoval"
    },
    **output_json,
    "contacts": [
        {
            "firstName": FIRST_NAME,
            "lastName": LAST_NAME,
            "isPrimary": "Y",
            "fullName": FULL_NAME,
            "organizationName": ORGANIZATION_NAME,
            "email": EMAIL,
            "phone2": PHONE,
            "status": {
                "value": "A",
                "text": "Active"
            },
            "type": {
                "value": "Applicant",
                "text": "Applicant"
            }
        }
    ]
})

response = requests.request("POST", URL, headers=HEADERS, data=PAYLOAD)
print(response.text)

# Get the record ID - This isn't returning a record ID... just NONE
record_id = response.json().get("id")
print(record_id)

record_id = "TRPA-25HIS-00000-00085"

HEADERS["Content-Type"] = "multipart/form-data"
URL = f"https://apis.accela.com/v4/records/{record_id}/documents"

files = {"file": open(DOCUMENT_PATH, "rb")}
filename = files["file"].name
basename = os.path.basename(filename)

data = {
    "recordId": record_id,
    "description": DOC_DESCRIPTION,
    "entityType": "Record",
    "fileName": basename,
    "type": "application/pdf",
}

#This is failing with an error that says: We are experiencing internal server error. Please contact Agency Administrator for assistance
response = requests.post(URL, headers=HEADERS, files=files, data=data)
files["file"].close()
print(response.text)

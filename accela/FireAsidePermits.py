import requests
import json
import os

os.system('cls')

URL = "https://apis.accela.com/v4/records"
AUTH_URL = "https://auth.accela.com/oauth2/token"

# Input variables from FireAside.  
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
# End of all of the input vars. Verify that they have all of these.


# Authentication Credentials
CLIENT_ID = "637946576467549438"
CLIENT_SECRET = "b6523f19a56047f8a1b8220c146b9178"
GRANT_TYPE = "password"
USERNAME = "FIREASIDE"
PASSWORD = "123456FA!"
AGENCY_NAME = "TRPA"
ENVIRONMENT = "NONPROD1"  # Set to "PROD" for production
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
else:
    print("Error:", AUTH_RESPONSE.status_code, AUTH_RESPONSE.text)
    exit()  # Exit if token generation fails

# Set the header
HEADERS = {
    "Authorization": f"{ACCESS_TOKEN}",
    "Content-Type": "application/json"
}

# Get the address record for the parcel
address_url = f"https://apis.accela.com/v4/parcels/{APN}/addresses"
address_response = requests.request("GET", address_url, headers=HEADERS)
input_json = json.loads(address_response.text)
address_json = {
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

# Define the URL
owner_url = f"https://apis.accela.com/v4/parcels/{APN}/owners"
owner_response = requests.get(owner_url, headers=HEADERS)

# Check if the response is successful
if owner_response.status_code == 200:
    # Parse the JSON response
    owner_data = owner_response.json()['result'][0]  # Get the first owner (or adjust as needed)

    # Prepare the formatted JSON structure
    formatted_owner_data = [
        {
            "email": "",
            "fax": "",
            "firstName": owner_data['firstName'].strip(),
            "fullName": owner_data['fullName'],
            "id": owner_data['id'],
            "isPrimary": owner_data['isPrimary'],
            "lastName": owner_data['lastName'],
            "mailAddress": {
                "addressLine1": owner_data['mailAddress']['addressLine1'],
                "addressLine2": owner_data['mailAddress']['addressLine2'],
                "addressLine3": "",
                "city": owner_data['mailAddress']['city'],
                "country": {
                    "text": "USA",
                    "value": "US"
                },
                "postalCode": owner_data['mailAddress']['postalCode'],
                "state": {
                    "text": owner_data['mailAddress']['state']['text'],
                    "value": owner_data['mailAddress']['state']['value']
                }
            },
            "middleName": "",
            "parcelId": "",
            "phone": "",
            "phoneCountryCode": "",
            "recordId": {
                "customId": "",
                "id": "",
                "serviceProviderCode": "",
                "trackingId": 0,
                "value": ""
            },
            "refOwnerId": 0,
            "status": {
                "text": owner_data['status']['text'],
                "value": owner_data['status']['value']
            },
            "taxId": "",
            "title": "",
            "type": "Owner"
        }
    ]

    # Convert to JSON
    owner_json = json.dumps(formatted_owner_data, indent=4)

else:
    print(f"Error fetching data: {owner_response.status_code}")


URL = "https://apis.accela.com/v4/records"
payload = json.dumps({
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
    **address_json,
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
response = requests.request("POST", URL, headers=HEADERS, data=payload)

# Extract the 'id' field.  Jason and Don, this is the TRPA Permit #
data = response.json()
record_id = data['result']['id']
print(record_id)

# Now associate the owner record with that record ID
owner_url = f"https://apis.accela.com/v4/records/{record_id}/owners"
response = requests.request("POST", owner_url, headers=HEADERS, data=owner_json)


#Everything works above this!!!  The following fails with a permission error.  Need to work with Accela on this
#Now let's finally attach the document.  
url = "https://apis.accela.com/v4/records/{record_id}/documents"
HEADERS = {
    "Authorization": f"{ACCESS_TOKEN}",
    'Content-Type': 'multipart/form-data'
}

# Prepare the file and fileInfo
file_path = "c:/temp/alert.gif"  # Replace with your actual file path
file_info = [
    {
        "serviceProviderCode": "TRPA",
        "fileName": "alert.gif",
        "type": "image/gif",
        "description": "Sample attachment"
    }
]

# Open the file to be uploaded
with open(file_path, 'rb') as file:
    # Prepare the multipart form-data body
    files = {
        'uploadedFile': (file_path.split('/')[-1], file, 'image/gif'),
        'fileInfo': (None, str(file_info), 'application/json')
    }
    
    # Send the POST request
    response = requests.post(url, headers=HEADERS, files=files)

    # Check the response
    if response.status_code == 200:
        print("Document attached successfully!")
    else:
        print(f"Failed to attach document. Status Code: {response.status_code}")

print(response.text)

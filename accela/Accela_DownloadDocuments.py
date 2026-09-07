import requests
import json
import os

AUTH_PAYLOAD = {
    "client_id": "638769013925399246",
    "client_secret": "c4d2bfe32c9e40e9919f851236206db0",
    "grant_type": "password",
    "username": "GISUSER",
    "password": "123456GIS!!",
    "agency_name": "TRPA",
    "environment": "PROD",
    "scope": "records documents parcels" 
}
HEADERS = {
    "Content-Type": "application/json"
}

download_folder = r"C:\temp\accela_docs"

auth_url = "https://auth.accela.com/oauth2/token"

# Make the POST request to get the access token
AUTH_RESPONSE = requests.post(auth_url, data=AUTH_PAYLOAD, headers=HEADERS)

if AUTH_RESPONSE.status_code == 200:
    ACCESS_TOKEN = AUTH_RESPONSE.json().get("access_token")
    print("Successfully authenticated!")
else:
    print("Error:", AUTH_RESPONSE.status_code, AUTH_RESPONSE.text)

access_token = ACCESS_TOKEN 

HEADERS = {
    "Authorization": f"{ACCESS_TOKEN}",
    "Content-Type": "application/json"
}

#This is the Permit ID. TRPA uses a custom id and not the Record ID
custom_ids = [
    "TYCS2025-0908", "TYCS2025-0907", "TYCS2025-0906", "TYCS2025-0882", "TYCS2025-0872", "TYCS2025-0871",
    "TYCS2021-0608", "TYCS2021-0606", "TYCS2021-0595", "TYCS2021-0591", "TYCS2021-0129", "TYCS2021-0110"
]


# Headers
headers = {"Authorization": f"{access_token}"}

#This is just to see if we can find the records
base_url = "https://apis.accela.com/v4/records?customId={}"
record_ids = []

for custom_id in custom_ids:
    url = base_url.format(custom_id)
    response = requests.get(url, headers=HEADERS)

    if response.status_code == 200:
        data = response.json()
        results = data.get("result", [])
        if not results:
            print(f"No result found for: {custom_id}")
        else:
            for record in results:
                record_id = record.get("id", "N/A")
                record_ids.append(record_id)
    else:
        print(f"Failed to fetch record for {custom_id} — Status code: {response.status_code}")
        if response.status_code == 401:
            print(f"Unauthorized access (401) when fetching record for {custom_id}. Exiting.")
            exit()
print(record_ids)


#Now I have all of the Record_IDs!  Get all of the documentids for the recordid
document_ids = []
for record_id in record_ids:
    # Endpoint to get documents attached to a specific record
    doc_url = f"https://apis.accela.com/v4/records/{record_id}/documents"

    response = requests.get(doc_url, headers=HEADERS)

    if response.status_code == 200:
        docs = response.json().get("result", [])
        if not docs:
            print(f"No documents found for record ID: {record_id}")
        else:
            print(f"Document IDs for record {record_id}:")
            for doc in docs:
                document_id = doc.get("id")
                file_name = doc.get("fileName", "Unnamed")
                document_ids.append(document_id)
                
                download_url = f"https://apis.accela.com/v4/documents/{document_id}/download"
                file_response = requests.get(download_url, headers=HEADERS)
                print(f"- ID: {document_id}, File Name: {file_name}")

                if file_response.status_code == 200:
                    file_path = os.path.join(download_folder, file_name)
                    with open(file_path, "wb") as f:
                        f.write(file_response.content)
                    print(f"Saved to: {file_path}")
                else:
                    print(f"Failed to download doc {document_id}. Status: {file_response.status_code}")
    else:
        print(f"Failed to get documents for {record_id}. Status code: {response.status_code}")
            
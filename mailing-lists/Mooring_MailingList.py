"""
MailingList_MooringRegistrations.py
Created: December 12th, 2023
Last Updated: December 12th, 2023
Mason Bindl, Tahoe Regional Planning Agency

This python script was developed to 

This script uses Python 3.x and was designed to be used with 
the default ArcGIS Pro python enivorment ""C:/Program Files/ArcGIS/Pro/bin/Python/envs/arcgispro-py3/python.exe"", with
no need for installing new libraries.
"""
#--------------------------------------------------------------------------------------------------------#
# import packages and modules
# base packages
import os
import logging
import pandas as pd
# external connection packages
import sqlalchemy as sa
from sqlalchemy.engine import URL
from sqlalchemy import create_engine
# ESRI packages
from arcgis.features import FeatureSet, GeoAccessor, GeoSeriesAccessor
# email packages
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# set workspace and sde connections 
working_folder = "C:\GIS"
csvPath = os.path.join(working_folder, "MailingLists", "MailingList_MooringRegistration.csv")

# connect to bmp SQL dataabase
connection_string = "DRIVER={ODBC Driver 17 for SQL Server};SERVER=sql14;DATABASE=tahoebmpsde;UID=sde;PWD=staff"
connection_url = URL.create("mssql+pyodbc", query={"odbc_connect": connection_string})
engine = create_engine(connection_url)

##--------------------------------------------------------------------------------------#
## EMAIL SETTINGS ##
##--------------------------------------------------------------------------------------#
## EMAIL SETUP
# path to text file
fileToSend = csvPath
# email parameters
subject = "Mooring Registratoin Mailing List"
sender_email = "infosys@trpa.org"
# password = ''
receiver_email = "GIS@trpa.gov; mmiller@trpa.gov"

#---------------------------------------------------------------------------------------#
## FUNCTIONS ##
#---------------------------------------------------------------------------------------#

# send email with attachments
def send_mail(body):
    msg = MIMEMultipart()
    msg['Subject'] = subject
    msg['From'] = sender_email
    msg['To'] = receiver_email

    msgText = MIMEText('%s<br><br>Cheers,<br>GIS Team' % (body), 'html')
    msg.attach(msgText)

    attachment = MIMEText(open(fileToSend).read())
    attachment.add_header("Content-Disposition", "attachment", filename = os.path.basename(fileToSend))
    msg.attach(attachment)

    try:
        with smtplib.SMTP("mail.smtp2go.com", 25) as smtpObj:
            smtpObj.ehlo()
            smtpObj.starttls()
#             smtpObj.login(sender_email, password)
            smtpObj.sendmail(sender_email, receiver_email, msg.as_string())
    except Exception as e:
        logger.error(e)

# get owner info dataframe from BMP SQL Database
with engine.begin() as bmpConnect:
    dfParcels = pd.read_sql("SELECT * FROM [tahoebmpsde].[sde].[TblParcel]", bmpConnect)# BMP - create dataframes from tahoebmpsde
# dfParcels     = pd.read_sql("SELECT * FROM [tahoebmpsde].[sde].[TblParcel]", bmpConnect)
dfParcels.rename(columns={"APN_String": "APNs"}, inplace=True)

# get mooring data 
dfMoor = pd.read_json("https://laketahoeinfo.org/WebServices/GetAssignedMooringTags/JSON/e17aeb86-85e3-4260-83fd-a2b32501c476")

# create series of column values listing APNs indexed by registration number
records = pd.DataFrame(dfMoor.APNs.str.split(',').tolist(), index=dfMoor['RegistrationNumber']).stack()
# convert series to data frame
df = records.to_frame()

# format data frame
flattened = pd.DataFrame(df.to_records())
dfM = flattened.rename(columns={"RegistrationNumber": "Registration", "0": "APNs"})
# dfM.drop(['level_0'], axis=1, inplace=True)
dfM.APNs = dfM.APNs.str.strip()

# merge parcels and sql table on APN
df = pd.merge(dfParcels, dfM, on='APNs', how='right')

# specify fields to keep
## Add Physical Address Information and Registration Status
dfOut = df[['APNs',
            'Registration',
            'ParcelStreet',
            'ParcelCity',
            'ParcelZip',
            'OwnerName',
            'OwnerStreet',
            'OwnerCity',
            'OwnerState',
            'OwnerZip',
            'OwnerPhone',
            'OwnerEmail'      
            ]].copy()

dfOut.drop_duplicates(subset="APNs", inplace=True)


dfM2= dfMoor.rename(columns={"RegistrationNumber": "Registration"})
dfOut.merge(dfM2, how='left', on=['Registration','Registration'])

dfFinal = pd.merge(dfOut, dfM2, on='Registration', how='left')
dfFinal.rename(columns={"APNs_x": "APN"}, inplace=True)

dfFinal.drop_duplicates(subset="APN", inplace=True)

dfFinalOut = dfFinal[['APN',
            'Registration',
            'RegistrationStatus',
            'ParcelStreet',
            'ParcelCity',
            'ParcelZip',
            'OwnerName',
            'OwnerStreet',
            'OwnerCity',
            'OwnerState',
            'OwnerZip',
            'OwnerPhone',
            'OwnerEmail'   
            ]].copy()

dfFinalOut.to_csv(csvPath)

# send email with header based on try/except result
header = "Here's the Mooring Mailing List."
send_mail(header)
import arcpy
import pandas as pd

# Define the path to the spreadsheet and read it
spreadsheet_path = r"F:\GIS\DOCUMENTATION\metadata_updates.xlsx"
metadata_df = pd.read_excel(spreadsheet_path)
# Define the workspace or root folder where the datasets are stored
workspace = r"F:\GIS\PARCELUPDATE\Workspace\Vector.sde"
arcpy.env.workspace = workspace  # Set the ArcPy workspace

try:
    # Loop through each row in the spreadsheet
    for index, row in metadata_df.iterrows(): 
        dataset_name = row['Layer Name'] 
        description = row['Description'] 
        summary = row['Summary'] 
        title = row['Title'] 
        tags = row['Tags'] 
        credits = row['Credits'] 
        uselimitations = row['Use Limitations']
        path = row['Path']

        # Path to the dataset within the workspace 
        dataset_path = f"{workspace}\\{path}"
        print(dataset_path)
        # Check if the dataset exists 
        if arcpy.Exists(dataset_path): # Access the metadata for the dataset 
            metadata = arcpy.metadata.Metadata(dataset_path)
            # Update metadata fields from spreadsheet values 
            print(dataset_name)
            metadata.title = title 
            metadata.summary = summary 
            metadata.description = description 
            metadata.tags = tags 
            metadata.accessConstraints = uselimitations
            metadata.credits = credits
    # Save the changes to the metadata 
            metadata.save() 
            print(f"Metadata updated for {dataset_name}") 
        else: 
            print(f"Dataset {dataset_path} not found in sde database.")

except arcpy.ExectuteError as ae:
    print (f"An ArcPy erorr happened: {ae}")
except Exception as e:
    # Get line number of error
    exc_type, exc_obj, tb = sys.exc_info()
    fname = os.path.split(tb.tb_frame.f_code.co_filename)[1]
    f = tb.tb_frame
    lineno = tb.tb_lineno
    print("Error on line: " + {lineno})
    print("General error: " + {e})
    print({exc_type} +  {fname} + {tb.tb_lineno})
            
            
print("Metadata update process complete.")

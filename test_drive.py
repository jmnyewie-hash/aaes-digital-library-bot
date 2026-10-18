import os.path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

FOLDER_ID = "1kipRzamreuK0X43kXT1dzFp77MetA45G"


def authenticate():
    credentials = None

    if os.path.exists("token.json"):
        credentials = Credentials.from_authorized_user_file(
            "token.json", SCOPES
        )

    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                "credentials.json", SCOPES
            )

            credentials = flow.run_local_server(port=0)

        with open("token.json", "w") as token_file:
            token_file.write(credentials.to_json())

    return credentials


def main():
    try:
        credentials = authenticate()

        service = build("drive", "v3", credentials=credentials)

        folder = service.files().get(
            fileId=FOLDER_ID,
            fields="id, name, mimeType"
        ).execute()

        print("\nGoogle Drive authentication successful!")
        print("Folder name:", folder["name"])
        print("Folder ID:", folder["id"])

        results = service.files().list(
            q=f"'{FOLDER_ID}' in parents and trashed = false",
            pageSize=10,
            fields="files(id, name, mimeType)"
        ).execute()

        files = results.get("files", [])

        print("\nFiles found:")

        if not files:
            print("No accessible files found directly inside the folder.")
        else:
            for file in files:
                print(f"- {file['name']} ({file['mimeType']})")

        print("\nTEST PASSED")

    except Exception as error:
        print("\nTEST FAILED")
        print("Error:", error)


if __name__ == "__main__":
    main()
import boto3
import zipfile
import os
import sys
from botocore.exceptions import ClientError

# -------------------------------
# CONFIGURATION - CHANGE THESE
# -------------------------------
APP_NAME = "MyPythonApp"
ENV_NAME = "MyPythonApp-env"
REGION = "us-east-1"  # Choose your AWS region
ZIP_FILE = "app.zip"  # Deployment package
S3_BUCKET = "my-python-app-deploy-bucket-12345"  # Must be globally unique

# -------------------------------
# STEP 1: ZIP YOUR APPLICATION
# -------------------------------
def create_zip():
    try:
        with zipfile.ZipFile(ZIP_FILE, 'w', zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk("app"):  # 'app' folder contains your code
                for file in files:
                    filepath = os.path.join(root, file)
                    arcname = os.path.relpath(filepath, "app")
                    zf.write(filepath, arcname)
        print(f"[OK] Created deployment package: {ZIP_FILE}")
    except Exception as e:
        print(f"[ERROR] Failed to create zip: {e}")
        sys.exit(1)

# -------------------------------
# STEP 2: UPLOAD TO S3
# -------------------------------
def upload_to_s3():
    s3 = boto3.client("s3", region_name=REGION)
    try:
        # Create bucket if not exists
        s3.create_bucket(
            Bucket=S3_BUCKET,
            CreateBucketConfiguration={"LocationConstraint": REGION}
        )
    except ClientError as e:
        if e.response['Error']['Code'] != 'BucketAlreadyOwnedByYou':
            print(f"[ERROR] S3 bucket creation failed: {e}")
            sys.exit(1)

    try:
        s3.upload_file(ZIP_FILE, S3_BUCKET, ZIP_FILE)
        print(f"[OK] Uploaded {ZIP_FILE} to S3 bucket {S3_BUCKET}")
    except Exception as e:
        print(f"[ERROR] Upload failed: {e}")
        sys.exit(1)

# -------------------------------
# STEP 3: DEPLOY TO ELASTIC BEANSTALK
# -------------------------------
def deploy_to_eb():
    eb = boto3.client("elasticbeanstalk", region_name=REGION)

    try:
        # Create application if not exists
        eb.create_application(ApplicationName=APP_NAME)
    except ClientError as e:
        if e.response['Error']['Code'] != 'InvalidParameterValue':
            print(f"[ERROR] Application creation failed: {e}")
            sys.exit(1)

    # Create application version
    try:
        eb.create_application_version(
            ApplicationName=APP_NAME,
            VersionLabel="v1",
            SourceBundle={"S3Bucket": S3_BUCKET, "S3Key": ZIP_FILE}
        )
        print("[OK] Created application version v1")
    except Exception as e:
        print(f"[ERROR] App version creation failed: {e}")
        sys.exit(1)

    # Create environment
    try:
        eb.create_environment(
            ApplicationName=APP_NAME,
            EnvironmentName=ENV_NAME,
            VersionLabel="v1",
            SolutionStackName="64bit Amazon Linux 2 v3.5.6 running Python 3.9"
        )
        print(f"[OK] Environment {ENV_NAME} is launching...")
    except Exception as e:
        print(f"[ERROR] Environment creation failed: {e}")
        sys.exit(1)

# -------------------------------
# MAIN EXECUTION
# -------------------------------
if __name__ == "__main__":
    if not os.path.exists("app"):
        print("[ERROR] 'app' folder with your Python code is missing.")
        sys.exit(1)

    create_zip()
    upload_to_s3()
    deploy_to_eb()
    print("[DONE] Deployment initiated. It may take a few minutes for AWS to make it live.")
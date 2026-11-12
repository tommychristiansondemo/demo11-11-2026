"""Cognito triggers.

pre_signup: demo environment - auto-confirm every self-registration so no
email verification is needed.
post_confirmation: place the user in the Cognito group matching the role
they chose at registration (custom:role = admin | employee). Federated
(SSO) users default to `employees`.
"""
import logging

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)
cognito = boto3.client("cognito-idp")

ROLE_TO_GROUP = {"admin": "admins", "employee": "employees"}


def pre_signup(event, context):
    event["response"]["autoConfirmUser"] = True
    attrs = event["request"].get("userAttributes", {})
    if "email" in attrs:
        event["response"]["autoVerifyEmail"] = True
    role = attrs.get("custom:role", "employee")
    if event.get("triggerSource") != "PreSignUp_ExternalProvider" and role not in ROLE_TO_GROUP:
        raise Exception("Invalid role. Choose 'employee' or 'admin'.")
    log.info("auto-confirmed signup", extra={"user": event.get("userName"), "role": role,
                                             "trigger": event.get("triggerSource")})
    return event


def post_confirmation(event, context):
    if event.get("triggerSource") not in ("PostConfirmation_ConfirmSignUp",):
        return event
    role = event["request"].get("userAttributes", {}).get("custom:role", "employee")
    group = ROLE_TO_GROUP.get(role, "employees")
    cognito.admin_add_user_to_group(UserPoolId=event["userPoolId"], Username=event["userName"], GroupName=group)
    log.info("added user to group", extra={"user": event["userName"], "group": group})
    return event

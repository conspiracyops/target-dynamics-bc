import requests
import json
from datetime import datetime, timedelta

class DynamicsAuth(requests.auth.AuthBase):
    def __init__(self, target):
        self.__config = dict(target.config)
        self.__config_path = target._config_file_path
        self.__client_id = self.__config["client_id"]
        self.__client_secret = self.__config["client_secret"]
        self.__redirect_uri = self.__config["redirect_uri"]
        self.__refresh_token = self.__config["refresh_token"]

        self.__session = requests.Session()
        self.__access_token = None
        self.__expires_at = None

    def ensure_access_token(self):
        if self.__access_token is None or self.__expires_at <= datetime.utcnow():
            response = self.__session.post(
                "https://login.microsoftonline.com/common/oauth2/token",
                data={
                    "client_id": self.__client_id,
                    "client_secret": self.__client_secret,
                    "redirect_uri": self.__redirect_uri,
                    "refresh_token": self.__refresh_token,
                    "grant_type": "refresh_token"
                },
            )

            if response.status_code != 200:
                raise Exception(response.text)

            data = response.json()

            self.__access_token = data["access_token"]
            self.__config["refresh_token"] = data["refresh_token"]
            self.__config["access_token"] = data["access_token"]

            with open(self.__config_path, "w") as outfile:
                json.dump(self.__config, outfile, indent=4)

            self.__expires_at = datetime.utcnow() + timedelta(
                seconds=int(data["expires_in"]) - 10
            )  # pad by 10 seconds for clock drift

    def __call__(self, r):
        self.ensure_access_token()
        r.headers["Authorization"] = "Bearer {}".format(self.__access_token)
        return r
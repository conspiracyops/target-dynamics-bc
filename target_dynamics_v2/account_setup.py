"""Client for the Precoro AccountSetup microservice (precoro-ms-integrations).

On export (Precoro -> Dynamics BC) the target creates master-data records in BC.
For those records to be updatable from either side afterwards, the mapping between
the Precoro entity id and the Dynamics BC id must live in the microservice table
hotglue_account_setup_integration_data, otherwise imports have nothing to match against.

Flow (mirrors target-precoro's proven search + patch, which is the design's
multi-legal-entity path):

  1. POST /api/hotglue/account_setup/search  -> get-or-create the row for this
     (integrationId, legalEntityId). The microservice reuses one external_id across a
     supplier's legal entities (matched by name + mapField) and writes integrationId.
     Returns {externalId, precoroId}.
  2. PATCH /api/hotglue/account_setup/{external_id} with {entityId} -> set precoroId on
     every row sharing that external_id. Only sent when precoroId isn't set yet.

Why not POST /api/hotglue/account_setup (create): it always mints a fresh external_id,
so multi-legal-entity rows would not share one external_id (which get_account_setup in
the export ETL relies on). Search is the get-or-create-with-shared-external_id path.

Auth: X-PRECORO-AUTH HMAC signature + X-COMPANY-ID header (company_id is read from the
header, not the body). Signature = HMAC-SHA256(secret, f"{payload}.{company_id}"),
where payload is compact JSON for a body and empty for a GET.
"""

import hashlib
import hmac
import json

import requests
import singer

LOGGER = singer.get_logger()


class AccountSetupClient:
    # Precoro entity type ids (subset relevant to the BC export target).
    # Matches target-precoro's ENTITY_TYPE_MAP: suppliers=8, items=9.
    ENTITY_TYPE_MAP = {
        "Vendors": 8,
        "Items": 9,
    }

    def __init__(self, account_setup: dict, logger=None):
        self.account_setup = account_setup or {}
        self.logger = logger or LOGGER

    @property
    def enabled(self) -> bool:
        return bool(self.account_setup.get("enabled")) and bool(self._base_url)

    @property
    def _base_url(self) -> str:
        return (self.account_setup.get("url") or "").rstrip("/")

    def entity_type_for(self, stream_name: str) -> int:
        return self.ENTITY_TYPE_MAP.get(stream_name, 1)

    def _headers(self, payload: dict = None) -> dict:
        """Generate the HMAC signature headers for the AccountSetup microservice."""
        secret = str(self.account_setup.get("secret"))
        company_id = str(self.account_setup.get("companyId", ""))

        # Signature uses compact JSON for a body, empty string for GET.
        payload_json = json.dumps(payload, separators=(",", ":")) if payload is not None else ""
        string_to_sign = f"{payload_json}.{company_id}"
        signature = hmac.new(bytes(secret, "UTF-8"), string_to_sign.encode(), hashlib.sha256).hexdigest()

        return {
            "X-PRECORO-AUTH": signature,
            "X-COMPANY-ID": company_id,
        }

    def _raise_for_status(self, response: requests.Response, context: str) -> None:
        try:
            response.raise_for_status()
        except requests.HTTPError as err:
            self.logger.error(
                f"{context} failed: HTTP {response.status_code}. Response body: {response.text}"
            )
            raise err

    def search(
        self,
        integration_id: str,
        legal_entity_id,
        entity_type: int,
        name: str = None,
        map_field: str = None,
    ) -> dict:
        """Get-or-create the mapping row for this (integrationId, legalEntityId)."""
        payload = {
            "legalEntityId": int(legal_entity_id),
            "entityType": entity_type,
            "integrationType": self.account_setup.get("integrationType"),
            "integrationId": str(integration_id),
        }
        if name:
            payload["name"] = name
        if map_field:
            payload["mapField"] = map_field

        url = f"{self._base_url}/api/hotglue/account_setup/search"
        self.logger.info(f"POST {url} with payload: {payload}")
        response = requests.post(url, json=payload, headers=self._headers(payload), timeout=15)
        self._raise_for_status(response, "AccountSetup search")
        return response.json()

    def patch(self, external_id: str, precoro_id, name: str = None, map_field: str = None) -> dict:
        """Set precoroId (entityId) on all rows sharing external_id."""
        payload = {"entityId": int(precoro_id)}
        if name:
            payload["name"] = name
        if map_field:
            payload["mapField"] = map_field

        url = f"{self._base_url}/api/hotglue/account_setup/{external_id}"
        self.logger.info(f"PATCH {url} with payload: {payload}")
        response = requests.patch(url, json=payload, headers=self._headers(payload), timeout=15)
        self._raise_for_status(response, "AccountSetup patch")
        return response.json()

    def register_mapping(
        self,
        integration_id: str,
        precoro_id,
        legal_entity_id,
        entity_type: int,
        name: str = None,
        map_field: str = None,
    ) -> dict:
        """Register the Precoro<->BC mapping via search (+ patch to set precoroId).

        Idempotent: on re-runs search matches by integrationId and returns the existing
        row, and precoroId is patched only while it isn't set yet.
        """
        search_resp = self.search(integration_id, legal_entity_id, entity_type, name=name, map_field=map_field)
        external_id = (search_resp or {}).get("externalId")
        if not external_id:
            self.logger.warning(
                f"AccountSetup search returned no externalId for integrationId={integration_id}; "
                f"cannot set precoroId. Response: {search_resp}"
            )
            return search_resp

        current_precoro_id = search_resp.get("precoroId")
        if precoro_id is not None and str(current_precoro_id) != str(precoro_id):
            return self.patch(external_id, precoro_id, name=name, map_field=map_field)

        return search_resp

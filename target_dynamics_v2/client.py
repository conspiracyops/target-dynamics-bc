import json
import re
import requests

from target_dynamics_v2.mappers.base_mappers import BaseMapper
from target_hotglue.common import HGJSONEncoder
from typing import Dict, List, Optional
import singer


from target_dynamics_v2.auth import DynamicsAuth

LOGGER = singer.get_logger()


class DynamicsRequestError(Exception):
    pass


class DynamicsClient:
    ref_request_endpoints = {
        "Companies": "companies",
        "Accounts": "companies({companyId})/accounts",
        "Locations": "companies({companyId})/locations",
        "Items": "companies({companyId})/items",
        "Currencies": "companies({companyId})/currencies",
        "PaymentMethods": "companies({companyId})/paymentMethods",
        "Customers": "companies({companyId})/customers",
        "Vendors": "companies({companyId})/vendors",
        "Dimensions": "companies({companyId})/dimensions",
        "purchaseInvoices": "companies({companyId})/purchaseInvoices",
        "purchaseInvoicesDimensionSetLines": "companies({companyId})/purchaseInvoices({entityId})/dimensionSetLines",
        "purchaseInvoiceLinesDimensionSetLines": "companies({companyId})/purchaseInvoiceLines({entityId})/dimensionSetLines",
        "purchaseInvoiceLines": "companies({companyId})/purchaseInvoices({parentId})/purchaseInvoiceLines",
        "Journals": "companies({companyId})/journals",
        "Attachments": "companies({companyId})/attachments",
        "AttachmentsContent": "companies({companyId})/attachments(id={parentId})/attachmentContent",
        "purchaseCreditMemos": "companies({companyId})/purchaseCreditMemos",
        "purchaseCreditMemosDimensionSetLines": "companies({companyId})/purchaseCreditMemos({entityId})/dimensionSetLines",
        "purchaseCreditMemoLinesDimensionSetLines": "companies({companyId})/purchaseCreditMemoLines({entityId})/dimensionSetLines",
        "purchaseCreditMemoLines": "companies({companyId})/purchaseCreditMemos({parentId})/purchaseCreditMemoLines",
    }

    # Endpoints that require the custom Precoro API instead of the standard BC API.
    _CUSTOM_API_ENDPOINTS = {"purchaseInvoiceLines", "purchaseInvoices"}
    _CUSTOM_API_CREDIT_MEMO_ENDPOINTS = {"purchaseCreditMemos", "purchaseCreditMemoLines"}
    _CUSTOM_API_EXCLUDED = {"dimensionSetLines"}
    _CUSTOM_API_METHODS = {"POST", "PATCH"}

    def __init__(self, target) -> None:
        self.config = target.config
        self.auth = DynamicsAuth(target)
        self._custom_api_entities = None

        environment = self.config.get("environment_name", "Production")
        full_url = self.config.get("full_url")
        if full_url:
            self.url = full_url
            self.custom_api_url = f"https://api.businesscentral.dynamics.com/v2.0/{environment}/api/precoro/finance/v2.0/"
        else:
            tenant_id, env_name = self._resolve_environment(environment)
            self.url = f"https://api.businesscentral.dynamics.com/v2.0/{tenant_id}/{env_name}/api/v2.0/"
            self.custom_api_url = f"https://api.businesscentral.dynamics.com/v2.0/{tenant_id}/{env_name}/api/precoro/finance/v2.0/"

    def _resolve_environment(self, environment_name: str) -> tuple:
        """Return (aadTenantId, name) for the given environment.

        Handles the 'Name (Type)' format that the HotGlue UI may produce
        (e.g. 'Production (Production)' → matches BC env named 'Production').
        """
        session = self.get_auth()
        response = session.get("https://api.businesscentral.dynamics.com/environments/v1.1")
        response.raise_for_status()
        environments = response.json().get("value", [])

        name_to_match = environment_name
        if " (" in name_to_match:
            name_to_match = name_to_match.split(" (")[0].strip()

        chosen = next(
            (e for e in environments if e["name"].lower() == name_to_match.lower()),
            None,
        )
        if not chosen:
            raise Exception(
                f"Environment '{environment_name}' not found in Business Central. "
                f"Available: {[e['name'] for e in environments]}"
            )
        return chosen["aadTenantId"], chosen["name"]

    def get_auth(self):
        r = requests.Session()
        return self.auth(r)
    
    def _make_request(self, endpoint, method, data=None, params=None, headers=None, should_dump_json=True, base_url=None):
        request_headers = {"Content-Type": "application/json"}
        if headers:
            request_headers.update(headers)

        url = (base_url or self.url) + endpoint
        request_params = params or {}
        request = self.get_auth()
        request.headers.update(request_headers)
        
        if should_dump_json:
            data = json.dumps(data, cls=HGJSONEncoder) if data else None

        return request.request(
            method=method,
            url=url,
            params=request_params,
            data=data,
            verify=True
        )

    @staticmethod
    def _summarize_response(response: requests.Response, max_length: int = 500) -> str:
        content_type = response.headers.get("Content-Type", "unknown")
        body = (response.text or "").strip()
        if len(body) > max_length:
            body = f"{body[:max_length]}..."
        if not body:
            body = "<empty body>"
        return f"status={response.status_code}, content_type={content_type}, body={body}"

    def _parse_json_response(self, response: requests.Response, context: str) -> dict:
        try:
            return response.json()
        except requests.exceptions.JSONDecodeError as exc:
            summary = self._summarize_response(response)
            raise DynamicsRequestError(
                f"{context} returned a non-JSON response: {summary}"
            ) from exc
    
    def _validate_response(self, response: requests.Response) -> tuple[bool, str | None]:
        if response.status_code >= 400:
            try:
                response_data = self._parse_json_response(response, "Dynamics API request")
                msg = self.error_to_string(response_data.get("error"))
            except DynamicsRequestError as exc:
                msg = str(exc)
            return False, msg
        else:
            return True, None
        
    def _validate_batch_response(self, response: dict) -> tuple[bool, str | None]:
        if response["status"] >= 400:
            msg = response.get("body", {}).get("error")
            return False, msg
        else:
            return True, None

    def make_batch_request(self, requests_data: List[dict], transaction_type: str = "non_atomic"):
        """
        Performs a batch request against the API, any API endpoint can be used in batch requests.
        requests_data: list of requests, each containing a dict of url, method, headers and body
        transaction_type:
            'non_atomic' = the batch requests will continue to be processed even if one of them fail
            'atomic' = if one of the requests fails all the other requests will be rolled back. It's good
                        to be used when multiple requests are needed for one entity, for example updating
                        Customer and it's default dimensions
        """
        headers = {"Prefer": "odata.continue-on-error=true"}

        if transaction_type == "atomic":
            # Prefer: odata.continue-on-error=false makes the batch request stop processing requests if one of them fail
            # Isolation: snapshot makes the batch request atomic, if one of the requests fail the operation will be rolled back
            # it's good to be used when multiple requests are needed for one entity, for example updating Customer and it's default dimensions
            headers = {"Isolation": "snapshot", "Prefer": "odata.continue-on-error=false"}

        request_data = {"requests": []}

        for request in requests_data:
            req_headers = request.get("headers", {})
            data = {
                "method": request["method"],
                "url": request["url"],
                "headers": {
                    "Content-Type": "application/json",
                    "If-Match": "*",
                    **req_headers
                },
                "body": request.get("body", {})
            }
            if request_id := request.get("request_id"):
                data["id"] = request_id

            request_data["requests"].append(data)

        # --- TEMP DEBUG: log every outgoing sub-request body before sending ---
        # Remove after diagnosing HGI-7905 / job 20fAk. request_data["requests"] and the
        # eventual `responses` list share the same order/index, so index i here == index i below.
        for i, req in enumerate(request_data["requests"]):
            LOGGER.info(
                f"[DEBUG-HGI7905] batch#{i} id={req.get('id')} {req['method']} {req['url']} "
                f"body={json.dumps(req.get('body'), cls=HGJSONEncoder)}"
            )

        base_url = self.custom_api_url if self._requires_custom_api(requests_data) else None
        response = self._make_request("$batch", "POST", data=request_data, headers=headers, base_url=base_url)
        if response.status_code >= 400:
            summary = self._summarize_response(response)
            raise DynamicsRequestError(f"Dynamics batch request failed: {summary}")

        response_data = self._parse_json_response(response, "Dynamics batch request")
        responses = response_data.get("responses", [])

        # --- TEMP DEBUG: pair each failing response with the exact body that caused it.
        # Match by "id" first (OData $batch responses carry the sub-request id back),
        # fall back to positional index only if the request had no id set. ---
        requests_by_id = {req["id"]: req for req in request_data["requests"] if "id" in req}
        for i, resp in enumerate(responses):
            if resp.get("status", 0) >= 400:
                matching_request = requests_by_id.get(resp.get("id"))
                if matching_request is None and i < len(request_data["requests"]):
                    matching_request = request_data["requests"][i]
                LOGGER.error(
                    f"[DEBUG-HGI7905] response_id={resp.get('id')} idx={i} FAILED status={resp.get('status')} "
                    f"error={resp.get('body', {}).get('error')} "
                    f"request={json.dumps(matching_request, cls=HGJSONEncoder) if matching_request else '??'}"
                )
        # --- END TEMP DEBUG ---

        return responses

    def get_entities(self, record_type: str, url_params: Optional[dict] = {}, filters: Optional[Dict[str, List]] = {}, expand: str = None):
        """"Uses batch request to get data because the url can be of any length, allowing for long filters"""
        endpoint = self.ref_request_endpoints[record_type].format(**url_params)
        entity_filters = []

        if expand:
                expand = f"$expand={expand}"
        
        for filter_field_name, filter_values in filters.items():
            if filter_values:
                entity_filters.append([f"{filter_field_name} eq {filter_value}" for filter_value in filter_values])

        if not entity_filters:
            entity_filters = [[]]

        requests_data = []

        # Dynamics doesn't support filtering on distinct fields, so we need to make one request for each filter type
        for entity_filter in entity_filters:
            if entity_filter:
                entity_filter = f"$filter={' or '.join(entity_filter)}"

            request_url = endpoint
            query_string = "&".join(filter(None, [expand, entity_filter]))
            if query_string:
                request_url += f"?{query_string}"

            requests_data.append({
                "url": request_url,
                "method": "GET",
            })

        batch_responses = self.make_batch_request(requests_data)
        
        entities = []

        for response in batch_responses:
            success, error_message = self._validate_batch_response(response)
            if not success:
                return success, error_message, entities
            entities += response.get("body", {}).get("value", [])
        
        return True, None, entities

    def get_companies(self):
        _, _, companies = self.get_entities("Companies")

        for company in companies:
            url_params = {"companyId": company["id"]}

            _, _, currencies = self.get_entities("Currencies", url_params)
            company["currencies"] = currencies
            
            _, _, payment_methods = self.get_entities("PaymentMethods", url_params)
            company["paymentMethods"] = payment_methods

            _, _, dimensions = self.get_entities("Dimensions", url_params, expand="dimensionValues")
            company["dimensions"] = dimensions

            _, _, accounts = self.get_entities("Accounts", url_params)
            company["accounts"] = accounts

            _, _, locations = self.get_entities("Locations", url_params)
            company["locations"] = locations

        return True, None, companies
    
    def get_existing_entities_for_records(self, companies_reference_data: List[Dict], record_type: str, records: List[Dict], filter_mappings: List[Dict], expand: Optional[str] = None) -> Dict[str, List]:
        """Maps records to companies and returns a list of entities based on 'records'"""
        
        # we need to map the company to query the existing customers
        company_entities_mapping = {}

        for record in records:
            company = BaseMapper.get_company_from_record(companies_reference_data, record)
            if not company:
                continue

            if company["id"] not in company_entities_mapping.keys():
                company_entities_mapping[company["id"]] = {}
            
            for filter_mapping in filter_mappings:
                filter_field_from = filter_mapping["field_from"]
                filter_field_to = filter_mapping["field_to"]
                filter_field_should_quote = filter_mapping["should_quote"]

                if filter_field_to not in company_entities_mapping[company["id"]]:
                    company_entities_mapping[company["id"]][filter_field_to] = []

                if rec_value := record.get(filter_field_from):
                    # escape odata string
                    rec_value = DynamicsClient.escape_odata_string(rec_value)
                    if filter_field_should_quote:
                        rec_value = f"'{rec_value}'"
                    company_entities_mapping[company["id"]][filter_field_to].append(rec_value)

        existing_company_entities = {}
        # make requests to get existing entities for each company from Dynamics
        for company_id in company_entities_mapping:
            url_params = { "companyId": company_id }
            _, _, entities = self.get_entities(
                record_type,
                url_params=url_params,
                filters=company_entities_mapping[company_id],
                expand=expand
            )
            if company_id not in existing_company_entities.keys():
                existing_company_entities[company_id] = []
            existing_company_entities[company_id] += entities

        return existing_company_entities
    
    @staticmethod
    def create_default_dimensions_requests(record_type: str, company_id: str, entity_id: str, default_dimensions: List[dict]):
        """
        If the Entity already exists we cannot create/update defaultDimensions for it.
        We need to send a separate request for it
        """
        requests = []

        for default_dimension in default_dimensions:
            endpoint = DynamicsClient.ref_request_endpoints[record_type] + "({entityId})/defaultDimensions"
            endpoint = endpoint.format(companyId=company_id, entityId=entity_id)
            request_params = {
                "url": endpoint,
                "method": "POST"
            }

            if default_dimension_id := default_dimension.pop("id", None):
                request_params = {
                    "url": f"{endpoint}({default_dimension_id})",
                    "method": "PATCH"
                }
            requests.append({"payload": default_dimension, "request_params": request_params})

        return requests
    
    @staticmethod
    def create_dimension_set_lines_requests(record_type: str, company_id: str, entity_id: str, dimensions_set_lines: List[dict], existing_dimension_set_lines: Optional[List[dict]]=[]):
        """
        Create the requests for upserting dimension set lines for a given entity
        """
        requests = []

        for dimension_set_line in dimensions_set_lines:
            dimension_id = dimension_set_line["id"]
            dimension_value_id = dimension_set_line["valueId"]

            found_existing_dimension = next((existing_dimension for existing_dimension in existing_dimension_set_lines if existing_dimension["id"] == dimension_id), None)

            endpoint = DynamicsClient.ref_request_endpoints[record_type]
            endpoint = endpoint.format(companyId=company_id, entityId=entity_id)
            
            if not found_existing_dimension:
                request_params = {
                    "url": endpoint,
                    "method": "POST" 
                }
                body = { "id": dimension_id, "valueId": dimension_value_id }
            else:
                request_params = {
                    "url": f"{endpoint}({dimension_id})",
                    "method": "PATCH" 
                }
                body = { "valueId": dimension_value_id }

            requests.append({ **request_params, "body": body})

        return requests
    
    @staticmethod
    def get_entity_upsert_request_params(record_type: str, company_id: str, entity_id: str = None, url_params: Optional[dict] = {}, request_id: str = None):
        endpoint = DynamicsClient.ref_request_endpoints[record_type]
        endpoint = endpoint.format(companyId=company_id, **url_params)
        request_params = {
            "url": endpoint,
            "method": "POST"
        }

        if entity_id:
            if record_type == "purchaseInvoiceLines":
                url = f"companies({company_id})/purchaseInvoiceLines({entity_id})"
            elif record_type == "purchaseCreditMemoLines":
                url = f"companies({company_id})/purchaseCreditMemoLines({entity_id})"
            else:
                url = f"{endpoint}({entity_id})"
                
            request_params = {
                "url": url,
                "method": "PATCH"
            }
        
        if request_id:
            request_params["request_id"] = request_id

        return request_params

    @property
    def custom_api_entities(self) -> Dict[str, set]:
        """Entity name -> set of field names exposed by the tenant's Precoro custom API extension.

        Versions of the AL extension differ between tenants (e.g. credit memo entities or
        the purchaseOrderNumber line field may be missing), so the actual capabilities are
        probed from $metadata once per run and cached.
        """
        if self._custom_api_entities is None:
            entities: Dict[str, set] = {}
            if self.config.get("use_custom_api"):
                try:
                    response = self._make_request("$metadata", "GET", base_url=self.custom_api_url)
                    if response.status_code == 200:
                        for match in re.finditer(r'<EntityType Name="([^"]+)"[^>]*>(.*?)</EntityType>', response.text, re.S):
                            entities[match.group(1)] = set(re.findall(r'<Property Name="([^"]+)"', match.group(2)))
                    else:
                        LOGGER.warning(f"Custom API metadata probe returned status {response.status_code}")
                except Exception as exc:
                    LOGGER.warning(f"Failed to probe custom API metadata: {exc}")
                LOGGER.info(f"Custom API entities: {sorted(entities)}")
            self._custom_api_entities = entities
        return self._custom_api_entities

    def custom_api_entity_has_field(self, entity: str, field: str) -> bool:
        return field in self.custom_api_entities.get(entity, set())

    @property
    def custom_api_supports_credit_memos(self) -> bool:
        return "purchaseCreditMemo" in self.custom_api_entities

    def _requires_custom_api(self, requests_data: List[dict]) -> bool:
        """Check if any request in a batch targets purchase invoice endpoints requiring the custom API."""
        if not self.config.get("use_custom_api"):
            return False

        for req in requests_data:
            url = req.get("url", "")
            method = req.get("method")
            if method not in self._CUSTOM_API_METHODS:
                continue
            if any(ex in url for ex in self._CUSTOM_API_EXCLUDED):
                continue
            if any(ep in url for ep in self._CUSTOM_API_ENDPOINTS):
                LOGGER.info(f"Custom API match: Method={method}, URL={url}")
                return True
            if (
                any(ep in url for ep in self._CUSTOM_API_CREDIT_MEMO_ENDPOINTS)
                and self.custom_api_supports_credit_memos
            ):
                LOGGER.info(f"Custom API match (credit memo): Method={method}, URL={url}")
                return True
        return False

    @staticmethod
    def escape_odata_string(value: Optional[str]) -> str:
        """Escape single quotes in OData string literals by doubling them."""
        if value is None:
            return ''
        return str(value).replace("'", "''")

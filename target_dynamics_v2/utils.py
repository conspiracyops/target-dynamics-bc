from typing import TypedDict, List

class InvalidConfigurationError(Exception):
    pass

class DimensionDefinitionNotFound(InvalidConfigurationError):
    pass

class InvalidCustomFieldDefinition(InvalidConfigurationError):
    pass

class InvalidInputError(Exception):
    pass

class RecordNotFound(InvalidInputError):
    pass

class DuplicatedRecord(InvalidInputError):
    pass

class MissingField(InvalidInputError):
    pass

class InvalidFieldValue(InvalidInputError):
    pass

class CompanyNotFound(InvalidInputError):
    pass

class InvalidDimensionValue(InvalidInputError):
    pass

class Currency(TypedDict):
    id: str
    code: str
    displayName: str
    symbol: str
    amountDecimalPlaces: str
    amountRoundingPrecision: float
    lastModifiedDateTime: str

class PaymentMethod(TypedDict):
    id: str
    code: str
    displayName: str
    lastModifiedDateTime: str

class DimensionValue(TypedDict):
    id: str
    code: str
    dimensionId: str
    displayName: str

class Dimension(TypedDict):
    id: str
    code: str
    displayName: str
    dimensionValues: List[DimensionValue]

class Account(TypedDict):
    id: str
    number: str
    displayName: str
    category: str
    subCategory: str
    blocked: bool
    accountType: str
    lastModifiedDateTime: str

class Location(TypedDict):
    id: str
    code: str
    displayName: str
    contact: str
    addressLine1: str
    addressLine2: str
    city: str
    state: str
    country: str
    postalCode: str
    phoneNumber: str
    email: str
    website: str
    lastModifiedDateTime: str
class Company(TypedDict):
    id: str
    name: str
    displayName: str
    businessProfileId: str
    currencies: List[Currency]
    paymentMethods: List[PaymentMethod]
    dimensions: List[Dimension]
    accounts: List[Account]
    locations: List[Location]

class ReferenceData(TypedDict):
    companies: List[Company]

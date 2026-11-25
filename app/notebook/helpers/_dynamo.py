"""One DynamoDB reader for read_dynamodb, dynamodb_schema, read_dynamodb_query and
the SQL engine (scan and PartiQL).

Items come back exactly as boto3's resource layer returns them (Decimal). The SQL
layer converts to float and caches; read_dynamodb keeps Decimal.

Every page is preceded by `check()`. By default it raises the SQL run guard's
message when the run guard fired (timeout or Stop), so a cell ends at a page
boundary. Outside a cell there is no guard and nothing is checked.
Underscore-prefixed so helpers/__init__ does not export it to notebook cells.
"""
from .. import aws_clients as _aws


def _default_check():
    from ..run_guard import current_guard
    guard = current_guard()
    if guard is not None and guard.fired:
        raise RuntimeError(guard.error_message())


def scan_items(table_name, region=None, *, limit=None, filter_condition=None,
               fill_limit=False, check=None):
    """Scan a table and return its items.

    No limit: follow LastEvaluatedKey to the end of the table.
    limit with fill_limit=False: ONE page with Limit=limit (read_dynamodb's
      documented behaviour).
    limit with fill_limit=True: keep reading until `limit` items or the end (a page
      can stop short of Limit at the 1 MB page cap); the SQL layer uses this.
    """
    check = check or _default_check
    table = _aws.aws_resource("dynamodb", region).Table(table_name)
    params = {}
    if limit and limit > 0:
        params["Limit"] = limit
    if filter_condition is not None:
        params["FilterExpression"] = filter_condition
    items = []
    check()
    response = table.scan(**params)
    items.extend(response.get("Items", []))
    if not limit:
        while "LastEvaluatedKey" in response:
            check()
            params["ExclusiveStartKey"] = response["LastEvaluatedKey"]
            response = table.scan(**params)
            items.extend(response.get("Items", []))
    elif fill_limit:
        while len(items) < limit and "LastEvaluatedKey" in response:
            check()
            params["Limit"] = limit - len(items)
            params["ExclusiveStartKey"] = response["LastEvaluatedKey"]
            response = table.scan(**params)
            items.extend(response.get("Items", []))
    return items


def sample_items(table_name, n, region=None):
    """A small sample for dynamodb_schema: one scan page of at most n items."""
    return scan_items(table_name, region, limit=max(1, n), fill_limit=False)


def query_items(table_name, key_condition, region=None, *, check=None):
    """Query by key condition and follow LastEvaluatedKey until exhausted (a
    single Query call stops at 1 MB)."""
    check = check or _default_check
    table = _aws.aws_resource("dynamodb", region).Table(table_name)
    params = {"KeyConditionExpression": key_condition}
    items = []
    while True:
        check()
        response = table.query(**params)
        items.extend(response.get("Items", []))
        if "LastEvaluatedKey" not in response:
            return items
        params["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def table_item_count(table_name, region=None):
    """Approximate item count (None when unavailable)."""
    try:
        return _aws.aws_resource("dynamodb", region).Table(table_name).item_count
    except Exception:
        return None


def partiql_items(statement, region=None, *, limit=None, check=None):
    """Run a PartiQL SELECT and return deserialized rows (Decimal numbers). With a
    limit the single response is returned (Limit is an API parameter); without one
    NextToken is followed to the end."""
    from boto3.dynamodb.types import TypeDeserializer
    check = check or _default_check
    client = _aws.aws_client("dynamodb", region)
    params = {"Statement": statement}
    if limit:
        params["Limit"] = limit
    check()
    response = client.execute_statement(**params)
    items = list(response.get("Items", []))
    if not limit:
        while "NextToken" in response:
            check()
            response = client.execute_statement(Statement=statement,
                                                NextToken=response["NextToken"])
            items.extend(response.get("Items", []))
    deserializer = TypeDeserializer()
    return [{k: deserializer.deserialize(v) for k, v in item.items()} for item in items]

# target-dynamics-v2

`target-dynamics-v2` is a Singer target for DynamicsV2.

Built by [Hotglue](https://hotglue.com) using singer-sdk.

## Installation

```bash
pipx install target-dynamics-v2
```

## Configuration

### Accepted Config Options

```bash
{
   "client_id": "Client ID",
   "client_secret": "Client Secret",
   "refresh_token": "Refresh Token obtained from oAuth",
   "org": "Organization ID, e.g., org32c52a45",
   "full_url": "Full Organization URL, e.g., https://org32c52a45.crm.dynamics.com"
}
```

A full list of supported settings and capabilities for this
target is available by running:

```bash
target-dynamics-v2 --about
```

### Configure using environment variables

This Singer target will automatically import any environment variables within the working directory's
`.env` if the `--config=ENV` is provided, such that config values will be considered if a matching
environment variable is set either in the terminal context or in the `.env` file.



### Executing the Target Directly

```bash
target-dynamics-v2 --version
target-dynamics-v2 --help
# Test using the "Carbon Intensity" sample:
tap-carbon-intensity | target-dynamics-v2 --config /path/to/target-dynamics-v2-config.json
```

## Developer Resources


### Initialize your Development Environment

```bash
pipx install poetry
poetry install
```

### Create and Run Tests

Create tests within the `target_dynamics_v2/tests` subfolder and
  then run:

```bash
poetry run pytest
```

You can also test the `target-dynamics-v2` CLI interface directly using `poetry run`:

```bash
poetry run target-dynamics-v2 --help
```


# Portfolio Assets

Custom Home Assistant integration that creates one device per asset with three entities:

* Price (fetched from a data source)
* Amount (user editable Number entity)
* Value (Price * Amount)

It also creates one Portfolio device with total value sensors (crypto, etf, fund, overall).

## Configuration (configuration.yaml)

```yaml
portfolio_assets:
  update_interval: 1800
  assets:
    - asset_id: btc
      name: Bitcoin
      kind: crypto
      source: binance
      instrument: BTCEUR
      amount_unit: BTC
```

### Options

Top level:

* `update_interval` (int, seconds, optional, default `1800`)
* `assets` (list, optional, default `[]`)

Per asset:

* `asset_id` (string, required)
  Used in unique ids and entity ids. Must be unique.
* `name` (string, required)
  Device name in Home Assistant.
* `kind` (string, required)
  One of: `crypto`, `etf`, `fund`
* `source` (string, required)
  One of: `binance`, `boerse_frankfurt`, `wienerboerse_oekb`
* `instrument` (string, required)

  * `binance`: symbol like `BTCEUR`, `ETHEUR`, `ADAEUR`
  * `boerse_frankfurt`: ISIN like `IE00B4L5Y983`
  * `wienerboerse_oekb`: ISIN like `AT0000722582` or a full quote URL
* `amount_unit` (string, optional, default empty)
  Unit shown on the Amount entity (e.g. `BTC`, `EUNL`, `Anteile`)
* `mic` (string, optional, default `XETR`)
  Only used for `boerse_frankfurt`.


## Examples

### Crypto (Binance)

```yaml
portfolio_assets:
  update_interval: 900
  assets:
    - asset_id: btc
      name: Bitcoin
      kind: crypto
      source: binance
      instrument: BTCEUR
      amount_unit: BTC

    - asset_id: doge
      name: Dogecoin
      kind: crypto
      source: binance
      instrument: DOGEEUR
      amount_unit: DOGE
```

### ETFs (Börse Frankfurt / XETRA)

```yaml
portfolio_assets:
  assets:
    - asset_id: eunl
      name: iShares Core MSCI World (EUNL)
      kind: etf
      source: boerse_frankfurt
      instrument: IE00B4L5Y983
      mic: XETR
      amount_unit: EUNL

    - asset_id: eunm
      name: iShares MSCI EM (EUNM)
      kind: etf
      source: boerse_frankfurt
      instrument: IE00B4L5YC18
      mic: XETR
      amount_unit: EUNM
```

### Funds (Wiener Börse OeKB)

Using ISIN:

```yaml
portfolio_assets:
  assets:
    - asset_id: kepler_mix_solide
      name: KEPLER Mix Solide
      kind: fund
      source: wienerboerse_oekb
      instrument: AT0000722582
      amount_unit: Anteile
```

Using full quote URL:

```yaml
portfolio_assets:
  assets:
    - asset_id: kepler_mix_solide
      name: KEPLER Mix Solide
      kind: fund
      source: wienerboerse_oekb
      instrument: "https://www.wienerborse.at/en/market-data/funds-data-provided-by-oekb/quote/?ID_NOTATION=8595392&ISIN=AT0000722582&cHash=759f3e6de615d08c78b396a8a7e4d3f7"
      amount_unit: Anteile
```

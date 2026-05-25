# Downdetector API

API em Python/FastAPI para consultar o status de serviços monitorados pelo [Downdetector](https://downdetector.com). O projeto usa [Scrapling](https://github.com/D4Vinci/Scrapling) com navegador headless para lidar com páginas protegidas e expõe uma interface HTTP simples para integração com automações, dashboards e monitoramento.

## Recursos

- Consulta status por slug do Downdetector, como `whatsapp`, `youtube`, `instagram` ou `openai`.
- Retorna status normalizado: `ok`, `warning` ou `down`.
- Gera imagem JPEG em base64 do card do serviço na tela principal do Downdetector.
- Gera uma imagem JPEG única com os cards dos serviços mais críticos lado a lado.
- Suporta localidade/domínio configurável, como `downdetector.com.br`, `downdetector.com` e outros.
- Possui cache por serviço para reduzir chamadas ao Downdetector.
- Protege os endpoints de consulta com token via `Authorization: Bearer`.
- Inclui setup Docker, Docker Compose e Compose com Traefik/Let's Encrypt.

## Requisitos

Para execução local:

- Python 3.10+
- Chromium instalado pelo Playwright

Para execução com Docker:

- Docker
- Docker Compose

## Configuração

Copie o arquivo de exemplo e ajuste as variáveis:

```bash
cp .env.example .env
```

Exemplo mínimo:

```env
DOWNDETECTOR_LOCALE=br
BROWSER_LOCALE=pt-BR
CACHE_TTL_SECONDS=120
REQUEST_TIMEOUT_SECONDS=90
HEADLESS=true
API_TOKEN=troque-este-token
```

Variáveis principais:

| Variável | Descrição |
|----------|-----------|
| `API_TOKEN` | Token exigido nos endpoints protegidos. |
| `DOWNDETECTOR_LOCALE` | Localidade/domínio do Downdetector. Ex.: `br`, `com`, `co.uk`. |
| `DOWNDETECTOR_STATUS_PATH` | Opcional. Caminho customizado com `{service}` quando a localidade usa rota diferente. |
| `BROWSER_LOCALE` | Locale usado pelo navegador headless. Ex.: `pt-BR`. |
| `CACHE_TTL_SECONDS` | Tempo de cache por serviço, em segundos. |
| `REQUEST_TIMEOUT_SECONDS` | Timeout das chamadas com navegador, em segundos. |
| `HEADLESS` | Define se o navegador roda em modo headless. |
| `TRAEFIK_HOST` | Domínio público usado no Compose com Traefik. |

Nunca publique seu `.env` real. O repositório já ignora esse arquivo por padrão.

## Execução Local

Instale as dependências:

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

Inicie a API:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

A documentação interativa fica disponível em:

```text
http://localhost:8000/docs
```

## Docker

Com Docker Compose:

```bash
docker compose up --build
```

A API ficará disponível em:

```text
http://localhost:8000
```

Sem Compose:

```bash
docker build -t downdetector-api .
docker run --rm -p 8000:8000 --env-file .env --shm-size=1g downdetector-api
```

O `--shm-size=1g` ajuda a manter o Chromium estável dentro do container.

## Docker Com Traefik

Use este modo quando você já tem um Traefik rodando em outro compose/stack.
Configure no `.env`:

```env
TRAEFIK_HOST=api.seudominio.com
```

Garanta que:

- o DNS de `TRAEFIK_HOST` aponta para o servidor;
- a rede Docker externa `traefik` já existe;
- o certificado TLS/certresolver `le` já está configurado no seu Traefik.

Depois execute:

```bash
docker compose -f docker-compose.traefik.yml up -d --build
```

Esse compose sobe apenas a API e publica o serviço no Traefik existente via labels.

## Autenticação

Os endpoints de consulta exigem token Bearer:

```http
Authorization: Bearer troque-este-token
```

O endpoint `/health` não exige autenticação para facilitar health checks.

## Endpoints

| Método | Rota | Autenticação | Descrição |
|--------|------|--------------|-----------|
| `GET` | `/health` | Não | Health check da API. |
| `GET` | `/critical/services` | Sim | Até 5 serviços mais críticos e imagem dos cards lado a lado. |
| `GET` | `/{service}/status` | Sim | Status completo do serviço. |
| `GET` | `/{service}/is-up` | Sim | Resposta booleana para disponibilidade. |

O `{service}` é o slug usado pelo Downdetector.

Exemplos:

```http
GET /youtube/status
GET /whatsapp/status?refresh=true
GET /instagram/is-up
GET /critical/services?count=5
```

Parâmetros:

| Parâmetro | Descrição |
|-----------|-----------|
| `refresh=true` | Ignora o cache e força uma nova consulta ao Downdetector. |
| `count=5` | Usado em `/critical/services`; aceita de 1 a 10 serviços. |

## Exemplo De Resposta

`GET /youtube/status`

```json
{
  "service": "youtube",
  "status": "ok",
  "label": "bom",
  "message": "User reports show no current problems with Youtube",
  "source_url": "https://downdetector.com/status/youtube/",
  "app_image_url": "https://...",
  "failure_graph_image_url": "data:image/jpeg;base64,...",
  "checked_at": "2026-05-24T19:09:09.191581Z",
  "cached": false
}
```

O campo `failure_graph_image_url` contém o card completo do serviço na tela principal do Downdetector, convertido para JPEG em base64.

`GET /critical/services`

```json
{
  "services": [
    {
      "service": "openai",
      "status": "down",
      "label": "ruim",
      "message": "Reportes dos usuários para OpenAI nas últimas 24 horas - enfrenta problemas",
      "source_url": "https://downdetector.com.br/fora-do-ar/openai/",
      "app_image_url": "https://..."
    }
  ],
  "image_url": "data:image/jpeg;base64,...",
  "source_url": "https://downdetector.com.br/",
  "checked_at": "2026-05-24T19:09:09.191581Z",
  "cached": false
}
```

O campo `image_url` contém uma única imagem JPEG em base64 com os cards críticos lado a lado.

## Status

| `status` | `label` | Significado |
|----------|---------|-------------|
| `ok` | `bom` | Sem problemas reportados. |
| `warning` | `instável` | Possíveis problemas. |
| `down` | `ruim` | Problemas reportados pelos usuários. |
| `unknown` | `bom` | Mensagem não reconhecida, tratada como serviço funcionando. |

## Observações

- O Downdetector pode bloquear ou alterar páginas sem aviso; por isso o scraper usa navegador headless e fallback pela tela principal.
- A primeira consulta pode levar mais tempo por causa da inicialização do Chromium.
- Cada serviço tem cache independente definido por `CACHE_TTL_SECONDS`.
- Este projeto não é afiliado ao Downdetector.

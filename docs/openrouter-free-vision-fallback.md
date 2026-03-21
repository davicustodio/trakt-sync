# OpenRouter Free Vision Fallback

Data da validacao: `2026-03-20`

## Objetivo

Definir uma cadeia deterministica de modelos gratuitos com entrada de imagem no OpenRouter para o webhook que identifica filmes e series a partir de imagens no WhatsApp.

## Lista validada no OpenRouter

A consulta autenticada ao endpoint oficial `https://openrouter.ai/api/v1/models` retornou 6 entradas zero-cost com suporte a imagem:

1. `openrouter/free`
2. `nvidia/nemotron-nano-12b-v2-vl:free`
3. `mistralai/mistral-small-3.1-24b-instruct:free`
4. `google/gemma-3-4b-it:free`
5. `google/gemma-3-12b-it:free`
6. `google/gemma-3-27b-it:free`

## Ordem recomendada para o webhook

### Cadeia principal

1. `mistralai/mistral-small-3.1-24b-instruct:free`
2. `google/gemma-3-27b-it:free`
3. `nvidia/nemotron-nano-12b-v2-vl:free`
4. `google/gemma-3-12b-it:free`
5. `google/gemma-3-4b-it:free`

### Reserva de emergencia

6. `openrouter/free`

## Racional da ordem

### 1. `mistralai/mistral-small-3.1-24b-instruct:free`

- Melhor aposta inicial entre os modelos free deterministas para tarefa multimodal geral.
- Janela de contexto longa (`128000`).
- Bom equilibrio entre capacidade e custo zero.

### 2. `google/gemma-3-27b-it:free`

- Variante Gemma free mais forte da lista.
- Melhor fallback quando a primeira tentativa vier com baixa confianca.
- Contexto longo (`131072`).

### 3. `nvidia/nemotron-nano-12b-v2-vl:free`

- Modelo explicitamente multimodal (`text+image+video->text`).
- Boa opcao intermediaria quando os generalistas falham.
- Descricao oficial e mais voltada a video/document intelligence, por isso ele entra depois dos dois acima para este caso de posters e frames de entretenimento.

### 4. `google/gemma-3-12b-it:free`

- Fallback medio, menor e potencialmente mais rapido que o Gemma 27B.
- Serve como quarta tentativa antes do ultimo fallback.

### 5. `google/gemma-3-4b-it:free`

- Ultimo fallback deterministico.
- Deve ser mantido para casos em que os modelos maiores estejam indisponiveis.

### 6. `openrouter/free`

- Nao usar como passo normal do fluxo.
- Ele roteia entre modelos gratuitos de forma variavel, o que dificulta benchmark, observabilidade e reproducao de erro.
- Vale somente como contingencia de disponibilidade.

## Politica de execucao no webhook

1. Tentar o modelo 1.
2. Se retornar erro HTTP, timeout, resposta vazia, JSON invalido ou confianca baixa, tentar o modelo seguinte.
3. Parar assim que houver:
   - `confidence >= 0.80`
   - `detected_title` preenchido
   - `media_type` coerente
4. Se todos falharem, responder ao usuario pedindo outra imagem.

## Regras de fallback

Avancar para o proximo modelo quando ocorrer qualquer um destes casos:

- timeout
- 5xx do provider
- resposta sem JSON valido
- `detected_title = null`
- `need_clarification = true`
- `confidence < 0.80`
- o TMDb nao conseguir normalizar o candidato com score suficiente

## Recomendacao de benchmark

Esta ordem e uma inferencia tecnica a partir do catalogo oficial do OpenRouter e do perfil dos modelos. Antes de travar a ordem em producao, rodar um benchmark rapido com:

- 20 posters
- 10 frames de filmes
- 10 frames de series
- 5 casos ambiguos

Se outro modelo apresentar mais acerto real no seu conjunto, reordenar a cadeia.

## Estrutura sugerida de configuracao

```json
{
  "openrouterVisionFallbacks": [
    "mistralai/mistral-small-3.1-24b-instruct:free",
    "google/gemma-3-27b-it:free",
    "nvidia/nemotron-nano-12b-v2-vl:free",
    "google/gemma-3-12b-it:free",
    "google/gemma-3-4b-it:free"
  ],
  "openrouterVisionEmergencyRouter": "openrouter/free"
}
```

## Fonte

- OpenRouter models endpoint: [https://openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models)
- OpenRouter image guide: [https://openrouter.ai/docs/guides/overview/multimodal/images](https://openrouter.ai/docs/guides/overview/multimodal/images)

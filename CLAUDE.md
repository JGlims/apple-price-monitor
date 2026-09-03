# apple-price-monitor

Monitora a loja de recondicionados da Apple US e avisa no Telegram quando aparece
um MacBook dentro dos meus critérios, ou quando um já visto baixa de preço.

## Stack

Python 3.12, só biblioteca padrão (`urllib`, `json`, `re`). **Sem dependências externas** —
é proposital: o GitHub Actions não precisa de `pip install`, e o script roda em qualquer
máquina com Python.

## Comandos canônicos

- **Testar sem efeito colateral:** `python3 monitor.py --dry-run`
- **Testar a notificação:** `python3 monitor.py --dry-run --force-notify`
- **Rodar de verdade:** `python3 monitor.py`
- **Não há** build, lint configurado nem suíte de testes ainda (ver Pendências).

## Como funciona

1. Baixa a página de recondicionados e extrai o JSON de inventário embutido nela.
2. Filtra por `criteria.json` (preço, memória, banda mínima, chips bloqueados).
3. Compara com `state.json` — o catálogo inteiro visto na execução anterior.
4. Notifica no Telegram só o que é novo ou baixou de preço.
5. O workflow commita o `state.json` de volta. **O repositório é o banco de dados.**

## Coisas que já quebraram — não repita

- **A Apple não tem API.** O JSON é extraído da página. `extract_products()` tem uma lista
  de padrões porque o nome da variável já mudou. Quando quebrar de novo, adicione o padrão
  novo lá; o script sai com código 2 e mensagem clara.
- **`previousPrice.raw_amount` é o preço ATUAL**, não o antigo — o "Was" fica na string irmã.
  Item sem desconto usa `currentPrice.raw_amount`. Os dois casos precisam ser tratados.
- **O `state.json` guarda o catálogo inteiro**, não só os itens que passam nos critérios.
  Guardar só os aprovados fazia tudo parecer novidade ao afrouxar um critério.
- **Silêncio é o pior modo de falha.** `notify()` levanta exceção se o secret faltar, e o
  workflow confere os secrets antes de rodar. Um bot mudo parece funcionando.
- **Erro engolido custa horas.** O `except` imprimia só `HTTP Error 400: Bad Request` e
  jogava fora o corpo da resposta, onde o Telegram diz o motivo exato. `_post()` agora lê
  `description` do JSON e coloca no texto do erro. Nunca capture `HTTPError` sem ler o corpo.
- **`parse_mode: Markdown` (legado) é frágil.** Um `_` ou `*` solto num título de produto
  derruba a mensagem inteira com 400. Usamos HTML com `esc()` em tudo que é interpolado, e
  se ainda assim falhar por parsing, reenviamos sem formatação.
- **Secret com espaço ou aspas coladas.** `TELEGRAM_CHAT_ID` passa por `.strip()` e
  `.strip('"\'')` porque colar do BotFather traz sujeira e o erro resultante ("chat not
  found") aponta para o lugar errado.
- **Critério nenhum é sábio o bastante para ser a única porta.** Quando nada passa, a
  mensagem lista os melhores abaixo do teto de preço. Estoque que rodou virava silêncio, e
  silêncio parece "nada mudou" quando na verdade era "olha isto aqui".

## Por que os critérios são esses

Para LLM local, dois números decidem: **memória unificada** define se o modelo carrega,
**largura de banda** define quão rápido ele escreve. Por isso `min_bandwidth: 250` elimina
toda a linha M5 base (153 GB/s) e `blocked_chips: ["M3 Pro"]` existe porque o M3 Pro tem
menos banda (150) que o M1 Pro de 2021 (205).

## Pendências

- [ ] Teste de `parse_listing()` com um JSON de exemplo salvo, sem depender da rede.
- [ ] Badge do status do workflow no README.

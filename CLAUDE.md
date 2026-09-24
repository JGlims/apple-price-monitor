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
- **Um erro de rede precisa dizer QUEM e PARA ONDE.** "400 Bad Request" nao diz de
  quem e o token nem para onde ia a mensagem. `_quem_sou()` pergunta ao `getMe` e
  `_mascara()` mostra o formato do chat_id sem imprimi-lo. Sem os dois, o caso em que
  o Telegram aceita o envio e a mensagem chega no chat de outro bot fica invisivel.
- **Erro engolido custa horas.** O `except` imprimia só `HTTP Error 400: Bad Request` e
  jogava fora o corpo da resposta, onde o Telegram diz o motivo exato. `_post()` agora lê
  `description` do JSON e coloca no texto do erro. Nunca capture `HTTPError` sem ler o corpo.
- **`parse_mode: Markdown` (legado) é frágil.** Um `_` ou `*` solto num título de produto
  derruba a mensagem inteira com 400. Usamos HTML com `esc()` em tudo que é interpolado, e
  se ainda assim falhar por parsing, reenviamos sem formatação.
- **Secret com espaço ou aspas coladas.** `TELEGRAM_CHAT_ID` passa por `.strip()` e
  `.strip('"\'')` porque colar do BotFather traz sujeira e o erro resultante ("chat not
  found") aponta para o lugar errado.
- **Um critério pode não descrever nenhuma máquina que existe.** `min_bandwidth: 250`
  combinado com `max_price: 1900` exigia um chip Pro por menos de US$ 1.900 — e não
  existe. O monitor rodou três semanas com dez execuções "success" e silêncio total,
  enquanto um M5 24GB caía de US$ 1.869 para US$ 1.699. Por isso `why()` tem **duas**
  regras (cabe no orçamento / chip Pro no alcance) em vez de um AND impossível, e a
  mensagem diz qual delas disparou.
- **Sucesso verde não é prova de nada.** O exit code 0 só diz que o script terminou.
  Por isso a segunda-feira leva `--digest`: um batimento semanal que torna
  "nada aconteceu" distinguível de "quebrou". Quinta continua calada.
- **Critério nenhum é sábio o bastante para ser a única porta.** Quando nada passa, a
  mensagem lista os melhores abaixo do teto de preço. Estoque que rodou virava silêncio, e
  silêncio parece "nada mudou" quando na verdade era "olha isto aqui".

## O dia em que a Apple mudar a página

Não é "se". O modo de falha perigoso **não** é a página sumir — isso estoura e
aparece. É a Apple renomear um campo: aí nada estoura, o valor vira 0, nada passa
nos critérios, e o monitor diz "0 novos" com toda a confiança. Foi exatamente
assim que ele ficou três semanas mudo.

Quatro camadas contra isso:

1. **`conferir_saude()`** olha o que hoje é 100% confiável — chip reconhecido,
   preço > 0, SSD > 0, tamanho do catálogo — e alerta se degradar. Os limiares
   foram medidos no catálogo real (382 brutos → 63 produtos), não chutados.
2. **A RAM fica fora da conferência de propósito.** A Apple não publica
   `tsMemorySize` nos MacBook Pro de 16": 51% de ausência é o normal. Esses itens
   aparecem numa lista separada quando cabem no teto, em vez de sumirem calados.
3. **Problema de saúde para a execução** (código 4): não notifica e **não grava o
   `state.json`**. Gravar um catálogo com preço zerado envenenaria a comparação
   para sempre — na execução seguinte tudo pareceria ter subido a partir de zero.
4. **`testes/teste_parse.py`** roda no CI contra `testes/amostra_apple.json`, uma
   amostra real salva. Mudança nos nomes dos campos falha ali, antes de ir para
   produção. Quando a Apple mudar de verdade: rode os testes, veja qual asserção
   caiu, conserte, e **regrave a amostra**.

Um alarme que nunca foi visto tocar não é um alarme — por isso o teste corrompe
o catálogo de propósito e exige que cada verificação dispare.

## Por que o preço aparece em três números

`US$ 1.699 · R$ 8.822 · pousado R$ 11.826`. O do meio é a conversão pura e
**engana**: entre a etiqueta e o Brasil entram o imposto de venda da Flórida
(cobrado no caixa), o IOF sobre o que passa no cartão, e o imposto de importação
de 50% sobre o que excede a isenção aérea de US$ 1.000 por pessoa — notebook não
entra em "bens de uso pessoal", é declarável. São quase R$ 3.000 de diferença.

A ordem importa: o IOF incide sobre a compra, não sobre o imposto de importação,
que você paga em reais na chegada. As premissas ficam no `criteria.json`, não no
código, e a mensagem imprime todas no rodapé — número sem a conta do lado vira
número mágico.

**A cotação nunca pode derrubar o monitor.** `cotacao()` tem duas fontes, e se
as duas falharem devolve `None` e a mensagem sai só em dólar. Também rejeita
valor fora de 3–20: se a API mudar de formato, é melhor não mostrar real nenhum
do que mostrar um número errado com cara de certo.

## Por que os critérios são esses

Para LLM local, dois números decidem: **memória unificada** define se o modelo carrega,
**largura de banda** define quão rápido ele escreve. Por isso `min_bandwidth: 250` elimina
toda a linha M5 base (153 GB/s) e `blocked_chips: ["M3 Pro"]` existe porque o M3 Pro tem
menos banda (150) que o M1 Pro de 2021 (205).

## Pendências

- [ ] Teste de `parse_listing()` com um JSON de exemplo salvo, sem depender da rede.
- [ ] Badge do status do workflow no README.

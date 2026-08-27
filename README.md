# Apple Refurb Monitor

Monitora a loja de recondicionados da Apple US e avisa no Telegram quando
aparece um MacBook que bate com os meus critérios, ou quando um que já
estava na lista baixa de preço.

Feito para uma compra específica: um MacBook Pro em Orlando, janeiro de 2027.

## Por que os critérios são esses

Para rodar modelos de IA localmente, dois números decidem tudo:

- **Memória unificada** — define *se* o modelo carrega. 24 GB roda até ~24B em Q4.
- **Largura de banda** — define *quão rápido* ele escreve, porque a cada token
  o modelo inteiro é lido da memória.

Por isso `min_bandwidth: 250` elimina toda a linha M5 base (153 GB/s) e deixa
passar M4 Pro (273), M5 Pro (307) e os Max. E `blocked_chips: ["M3 Pro"]`
existe porque o M3 Pro tem **menos** banda (150) que o M1 Pro de 2021 (205) —
é a pior compra da tabela.

## Rodar local

```bash
python3 monitor.py --dry-run   # só imprime, não notifica nem salva estado
python3 monitor.py             # notifica e grava state.json
```

## Notificação no Telegram

1. Fale com o [@BotFather](https://t.me/BotFather) → `/newbot` → guarde o token.
2. Mande qualquer mensagem para o seu bot.
3. Abra `https://api.telegram.org/bot<TOKEN>/getUpdates` e pegue o `chat.id`.
4. No GitHub: *Settings → Secrets and variables → Actions* → crie
   `TELEGRAM_TOKEN` e `TELEGRAM_CHAT_ID`.

## Agendamento

O workflow roda às segundas e quintas, 10h de Brasília, e também pelo botão
*Run workflow* na aba Actions. O `state.json` é commitado de volta pelo próprio
workflow — é assim que ele lembra o que já viu entre uma execução e outra.

## Limitação conhecida

A Apple não publica API de recondicionados. O script extrai o JSON embutido na
página, e o nome dessa variável já mudou algumas vezes. Se o monitor sair com
código 2 e a mensagem "o formato da página provavelmente mudou", é isso —
`extract_products()` tem uma lista de padrões justamente para sobreviver a essas
mudanças, e é lá que se adiciona o próximo.

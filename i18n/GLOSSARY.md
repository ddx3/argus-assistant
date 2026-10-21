# Argus translation: glossary of terms and rules

The source language is English: the interface text is written in English, and the English string is the translation
key. Russian is one of the nine interface languages and is translated like the others (`ru.json`). The product is a
desktop assistant for Windows and macOS: it listens to a conversation (the microphone and the system audio), looks at
the screen, and suggests an answer. It is used on work calls, lectures, exams, and interviews. People pay with money
from a balance (a top-up from $5, no subscription), so the words "tokens", "plan", and "subscription" do not appear in
new strings.

## Languages

| code | language | how to address the person |
|-----|------|----------------------------|
| en | English (en-US), the source | you |
| ru | Russian | ты |
| uk | the language with the code `uk` | ти |
| es | Spanish, neutral Latin American | tú |
| pt | Portuguese (pt-BR) | você |
| de | German | du |
| fr | French | tu |
| tr | Turkish | sen |
| pl | Polish | ty |

The voice of the product is friendly and short, with no bureaucratic wording. English has no formal "you", but the
tone of the source is direct and informal ("Write as is", "Press a combo…"): in every language that has a choice, the
informal address is used and stays informal everywhere.

## Terms (keep them the same everywhere)

| English | meaning | ru | es | pt | de | fr | tr | pl | uk |
|---|---|---|---|---|---|---|---|---|---|
| Them | the person on the other end of the call (the label of a line) | Собеседник | Ellos | Eles | Gegenüber | Interlocuteur | Karşı taraf | Rozmówca | Співрозмовник |
| Me | my line | Я | Yo | Eu | Ich | Moi | Ben | Ja | Я |
| System audio | everything that plays on the computer | Звук системы | Audio del sistema | Áudio do sistema | Systemton | Son du système | Sistem sesi | Dźwięk systemu | Звук системи |
| recognition | converting speech to text | распознавание | reconocimiento | reconhecimento | Erkennung | reconnaissance | tanıma | rozpoznawanie | розпізнавання |
| balance | money on the account in dollars, from which usage is charged; not seconds and not tokens | баланс | saldo | saldo | Guthaben | solde | bakiye | saldo | баланс |
| top-up | money put on the balance (from $5) | пополнение | recarga | recarga | Aufladung | recharge | yükleme | doładowanie | поповнення |
| session | one conversation with its own settings, materials, and history of answers | сессия | sesión | sessão | Sitzung | session | oturum | sesja | сесія |
| recording | a recorded conversation (the text of the lines), worked with in "Work with the recording"; the "Record" switch itself is a separate word: Запись (ru), Grabar (es), Gravar (pt), Aufnahme (de), Enregistrer (fr), Kayıt (tr), Nagrywanie (pl), Запис (uk) | запись | grabación | gravação | Aufnahme | enregistrement | kayıt | zapis, nagranie | запис |
| Hotkeys | key combinations | Хоткеи | Atajos | Atalhos | Tastenkürzel | Raccourcis | Kısayollar | Skróty | Гарячі клавіші |
| Materials | the user's files that the assistant takes into account | Материалы | Materiales | Materiais | Materialien | Documents | Materyaller | Materiały | Матеріали |
| Voice replies | spoken answers (the settings tab and the chart legend are called "Voice-over") | Озвучка ответов (the tab: Озвучка) | Respuestas por voz | Respostas por voz | Sprachausgabe | Réponses vocales | Sesli yanıtlar | Odpowiedzi głosowe | Озвучення |
| Think longer | a mode: a slower but more thorough answer | Думать дольше | Pensar más | Pensar mais | Länger nachdenken | Réfléchir plus | Daha uzun düşün | Myśl dłużej | Думати довше |
| Screen | reading the screen; a screen frame | Экран | Pantalla | Tela | Bildschirm | Écran | Ekran | Ekran | Екран |
| overlay | the window of answers on top of everything (ru: оверлей / общее окно) | общее окно | superposición | sobreposição | Overlay | surimpression | yer paylaşımı penceresi | nakładka | оверлей |
| voice | the voice of the spoken answers (ru: персонаж) | персонаж | voz | voz | Stimme | voix | ses | głos | голос |

(If a language has a more natural form, use it, but keep the same term the same everywhere.)

## Do not translate

Argus, Whisper, ElevenLabs, Windows, Enter, ctrl / alt / shift / win, commands
(`argus run --settings`, `argus sync-voices`, `argus import-voice`), PDF, Word,
PowerPoint, `{placeholders}` for values, HTML tags (`<b>`, `<code>`, `<a>`).
Language names are written in the language itself (русский, English, Deutsch…).

## What must not appear in a translation at all

The visible text does not name how the app is built (a project rule, "text for people"): there are no
words model / API / key / token / relay / provider / pipeline or their translations, neither in new strings nor in
translations of old ones. Instead of "tokens" use money and "balance", and instead of model names use the two answer
levels (the regular answer and "Think longer"). The name of someone else's company in examples ("interview at Google")
is replaced with one that is well known in that language's market: Google is the example in the English source, and
the Russian translation uses a Russian company.

## Form

- Labels on buttons and in the header stay short: a translation should be no more than a third
  longer than the English original, if possible.
- Lowercase English strings ("question", "thinking", "with screenshot") stay lowercase, because
  the styles themselves turn them into caps. CAPS in the original ("RECOGNITION IN PROGRESS") stays caps.
- The ellipsis "…" and the dash "—" are kept where they are, if the language allows them.
- `{n}`, `{name}`, `{error}` and other `{placeholders}` must stay in the translation exactly the same.
- A key with a tag: the translation has the same tags; the word order may change.

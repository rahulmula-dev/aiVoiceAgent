### test 1,2,3:


C:\\Users\\rmxhi\\Desktop\\Projects\\Ai VOICE agent> uv run run\_server.py

\[BOOT] LLM provider = Groq (llama-3.1-8b-instant)

\[MAIN] Starting modular voice pipeline server on port 5000

&#x20;      STT -> Deepgram nova-3

&#x20;      LLM -> Groq     llama-3.1-8b-instant

&#x20;      TTS -> ElevenLabs eleven\_flash\_v2\_5

&#x20;      Gate -> Redis    max 5 concurrent calls

\[MAIN] Warming up connection pools...

\[POOL/STT] Pre-warming 2 Deepgram connections...

\[POOL/TTS] ElevenLabs connection warmed (HTTP 401)

\[POOL/STT] 2/2 connections ready

\[GATE] Concurrency gate ready — max 5 concurrent calls

\[MAIN] Waiting for Twilio calls...

\[TWIML] /voice hit  -> wss://violate-partner-lurk.ngrok-free.dev/  call\_sid=CAab425793a66d4a11eba337495accb39b

\[WS] Twilio Media Stream connected

\[ORCH] Waiting for stream SID from Twilio...

\[TWILIO] Stream started  SID=MZb430600eb65b9e6f73025d6b52e483e3

\[ORCH] Governance ENABLED — language gate + restricted-topic filter active

\[RAG] LOCAL\_TEST mode — using mock embeddings (\[1.0]\*1536)

\[ORCH] RAG ENABLED — KnowledgeBase (pgvector) active

\[ORCH] Playing greeting...

\[ORCH] Pipeline active — call in progress

\[POOL/STT] Acquired pre-warmed connection (idle 237703ms)

\[STT] Connected to Deepgram STT (Nova-3)

\[STT] Speech recognised -> barge-in ('Hi.')

\[STT] Interim: 'Hi.'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('Hi, my name is')

\[STT] Interim: 'Hi, my name is'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('Hi, my name is राहुल and')

\[STT] Interim: 'Hi, my name is राहुल and'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('Hi, my name is राहुल and I am,')

\[STT] Interim: 'Hi, my name is राहुल and I am, I'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('Hi, my name is राहुल and I hav')

\[STT] Interim: 'Hi, my name is राहुल and I have, I want to take admission'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Segment: 'Hi, my name is राहुल and'

\[STT] Interim: 'I want to take admission in your'

\[STT] Speech recognised -> barge-in ('I want to take admission in co')

\[STT] Final (speech\_final): 'Hi, my name is राहुल and I want to take admission in college.'  \[lang=hi]

\[LLM] User said: 'Hi, my name is राहुल and I want to take admission in college.'

\[RAG] Injecting context (score=0.75, cat=Admissions)

\[TTS] Sentence: 'Hello Rahul, welcome to GD College.'

\[TTS] Sentence: 'To apply for admission, you can visit our website and fill o...'

\[STT] Speech recognised -> barge-in ('What programs do you offer me?')

\[STT] Interim: 'What programs do you offer me?'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('What')

\[STT] Interim: 'What'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('What programs do you offer')

\[STT] Interim: 'What programs do you offer'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('What programs do you offer?')

\[STT] Final (speech\_final): 'What programs do you offer?'  \[lang=en]

\[LLM] User said: 'What programs do you offer?'

\[RAG] Injecting context (score=0.73, cat=Fees)

\[TTS] Sentence: 'We offer a variety of programs, including our Massage Therap...'

\[STT] Speech recognised -> barge-in ('Thanks, goodbye.')

\[STT] Interim: 'Thanks, goodbye.'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('Thanks, goodbye.')

\[STT] Final (speech\_final): 'Thanks, goodbye.'  \[lang=en]

\[STT] 👋 Hangup phrase detected — call will end after farewell

\[LLM] User said: 'Thanks, goodbye.'

\[RAG] Injecting context (score=0.71, cat=General Info)

\[LLM] Shutting down

\[TTS] Sentence: 'It was nice speaking with you, Rahul.'

\[TTS] Sentence: 'Have a great day.'

\[TTS] Shutting down

\[TWILIO] Final mark received — playback drained

\[STT] Disconnected from Deepgram STT

\[STATUS] CallSid=CAab425793a66d4a11eba337495accb39b  status=completed  duration=119s  ts=1782467593

\[GATE] Released slot for CAab425793a66d4a11eba337495accb39b (completed), active=0

\[LOG] Transcript saved -> C:\\Users\\rmxhi\\Desktop\\Projects\\Ai VOICE agent\\logs\\transcripts\\2026-06-26\_09-51-15.json

\[LOG] Call summary saved -> C:\\Users\\rmxhi\\Desktop\\Projects\\Ai VOICE agent\\logs\\calls\\2026-06-26\_09-51-15\_MZb430600eb65b9e6f73025d6b52e483.json

\[ORCH] Call ended — all tasks complete



\[MAIN] Shutting down









test 4:
---

PS C:\\Users\\rmxhi\\Desktop\\Projects\\Ai VOICE agent> uv run run\_server.py

\[BOOT] LLM provider = Groq (llama-3.1-8b-instant)

\[MAIN] Starting modular voice pipeline server on port 5000

&#x20;      STT -> Deepgram nova-3

&#x20;      LLM -> Groq     llama-3.1-8b-instant

&#x20;      TTS -> ElevenLabs eleven\_flash\_v2\_5

&#x20;      Gate -> Redis    max 5 concurrent calls

\[MAIN] Warming up connection pools...

\[POOL/STT] Pre-warming 2 Deepgram connections...

\[POOL/TTS] ElevenLabs connection warmed (HTTP 401)

\[POOL/STT] 2/2 connections ready

\[GATE] Concurrency gate ready — max 5 concurrent calls

\[MAIN] Waiting for Twilio calls...

\[TWIML] /voice hit  -> wss://violate-partner-lurk.ngrok-free.dev/  call\_sid=CA311e06650df8793300ce4dc41691213a

\[WS] Twilio Media Stream connected

\[ORCH] Waiting for stream SID from Twilio...

\[TWILIO] Stream started  SID=MZce92939c6abc85c1769b63b4c974849d

\[ORCH] Governance ENABLED — language gate + restricted-topic filter active

\[RAG] LOCAL\_TEST mode — using mock embeddings (\[1.0]\*1536)

\[ORCH] RAG ENABLED — KnowledgeBase (pgvector) active

\[ORCH] Playing greeting...

\[ORCH] Pipeline active — call in progress

\[POOL/STT] Acquired pre-warmed connection (idle 21672ms)

\[STT] Connected to Deepgram STT (Nova-3)

\[STT] Speech recognised -> barge-in ('Tell me')

\[STT] Interim: 'Tell me'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('Tell me about your schedule.')

\[STT] Interim: 'Tell me about your schedule.'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('Tell me about your schedules.')

\[STT] Final (speech\_final): 'Tell me about your schedules.'  \[lang=en]

\[LLM] User said: 'Tell me about your schedules.'

\[RAG] Injecting context (score=0.73, cat=Academic)

\[TTS] Sentence: 'We offer flexible schedules including morning, afternoon, ev...'

\[TWILIO] Stream stopped

\[STATUS] CallSid=CA311e06650df8793300ce4dc41691213a  status=completed  duration=39s  ts=1782467838

\[GATE] Released slot for CA311e06650df8793300ce4dc41691213a (completed), active=0

\[STT] Disconnected from Deepgram STT

\[LLM] Shutting down

\[TTS] Shutting down

\[LOG] Transcript saved -> C:\\Users\\rmxhi\\Desktop\\Projects\\Ai VOICE agent\\logs\\transcripts\\2026-06-26\_09-56-40.json

\[LOG] Call summary saved -> C:\\Users\\rmxhi\\Desktop\\Projects\\Ai VOICE agent\\logs\\calls\\2026-06-26\_09-56-40\_MZce92939c6abc85c1769b63b4c97484.json

\[ORCH] Call ended — all tasks complete





### test 5:



PS C:\\Users\\rmxhi\\Desktop\\Projects\\Ai VOICE agent> uv run run\_server.py

\[BOOT] LLM provider = Groq (llama-3.1-8b-instant)

\[MAIN] Starting modular voice pipeline server on port 5000

&#x20;      STT -> Deepgram nova-3

&#x20;      LLM -> Groq     llama-3.1-8b-instant

&#x20;      TTS -> ElevenLabs eleven\_flash\_v2\_5

&#x20;      Gate -> Redis    max 5 concurrent calls

\[MAIN] Warming up connection pools...

\[POOL/STT] Pre-warming 2 Deepgram connections...

\[POOL/TTS] ElevenLabs connection warmed (HTTP 401)

\[POOL/STT] 2/2 connections ready

\[GATE] Concurrency gate ready — max 5 concurrent calls

\[MAIN] Waiting for Twilio calls...

\[TWIML] /voice hit  -> wss://violate-partner-lurk.ngrok-free.dev/  call\_sid=CA014e7e0b82018c846a4fc073803ade57

\[WS] Twilio Media Stream connected

\[ORCH] Waiting for stream SID from Twilio...

\[TWILIO] Stream started  SID=MZ581002ac6d6a3c671f7be148d156b443

\[ORCH] Governance ENABLED — language gate + restricted-topic filter active

\[RAG] LOCAL\_TEST mode — using mock embeddings (\[1.0]\*1536)

\[ORCH] RAG ENABLED — KnowledgeBase (pgvector) active

\[ORCH] Playing greeting...

\[ORCH] Pipeline active — call in progress

\[POOL/STT] Acquired pre-warmed connection (idle 11219ms)

\[STT] Connected to Deepgram STT (Nova-3)

\[STT] Speech recognised -> barge-in ('नमस्ते,')

\[STT] Interim: 'नमस्ते,'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('नमस्ते, मुझे course के')

\[STT] Interim: 'नमस्ते, मुझे course के'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('नमस्ते, मुझे course के बारे मे')

\[STT] Interim: 'नमस्ते, मुझे course के बारे में बताइए.'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Final (speech\_final): 'नमस्ते, मुझे course के बारे में बताइए.'  \[lang=hi]

\[LLM] User said: 'नमस्ते, मुझे course के बारे में बताइए.'

\[GOV-LANG] strike 1/3  lang=hi  conf=1.00  terminate=False

\[TTS] Sentence: 'I'm sorry, I'm programmed to assist in English only. Could y...'

\[STT] Speech recognised -> barge-in ('मुझे English नहीं आता मुझे हिं')

\[STT] Interim: 'मुझे English नहीं आता मुझे हिंदी'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('मुझे English नहीं आता है, मुझे')

\[STT] Final (speech\_final): 'मुझे English नहीं आता है, मुझे हिंदी आता है.'  \[lang=hi]

\[LLM] User said: 'मुझे English नहीं आता है, मुझे हिंदी आता है.'

\[GOV-LANG] strike 2/3  lang=hi  conf=1.00  terminate=False

\[TTS] Sentence: 'I'm sorry, I'm programmed to assist in English only. Could y...'

\[STT] Speech recognised -> barge-in ('हिंदी में बात की')

\[STT] Interim: 'हिंदी में बात की'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('हिंदी में बात कीजिए please.')

\[STT] Interim: 'हिंदी में बात कीजिए please.'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('हिंदी में बात कीजिए please.')

\[STT] Final (speech\_final): 'हिंदी में बात कीजिए please.'  \[lang=hi]

\[LLM] User said: 'हिंदी में बात कीजिए please.'

\[GOV-LANG] strike 3/3  lang=hi  conf=1.00  terminate=True

\[TTS] Sentence: 'I'm sorry, since I can only assist in English, I will have t...'

\[TTS] Shutting down

\[ORCH] Final mark not received within 8s — closing anyway

\[STATUS] CallSid=CA014e7e0b82018c846a4fc073803ade57  status=completed  duration=46s  ts=1782468017

\[GATE] Released slot for CA014e7e0b82018c846a4fc073803ade57 (completed), active=0

\[STT] Disconnected from Deepgram STT

\[LOG] Transcript saved -> C:\\Users\\rmxhi\\Desktop\\Projects\\Ai VOICE agent\\logs\\transcripts\\2026-06-26\_09-59-31.json

\[LOG] Call summary saved -> C:\\Users\\rmxhi\\Desktop\\Projects\\Ai VOICE agent\\logs\\calls\\2026-06-26\_09-59-31\_MZ581002ac6d6a3c671f7be148d156b4.json

\[ORCH] Call ended — all tasks complete



\[MAIN] Shutting down



### test 6:talked in accent and at last I had to hangup the call , no automatic cut .



PS C:\\Users\\rmxhi\\Desktop\\Projects\\Ai VOICE agent> uv run run\_server.py

\[BOOT] LLM provider = Groq (llama-3.1-8b-instant)

\[MAIN] Starting modular voice pipeline server on port 5000

&#x20;      STT -> Deepgram nova-3

&#x20;      LLM -> Groq     llama-3.1-8b-instant

&#x20;      TTS -> ElevenLabs eleven\_flash\_v2\_5

&#x20;      Gate -> Redis    max 5 concurrent calls

\[MAIN] Warming up connection pools...

\[POOL/STT] Pre-warming 2 Deepgram connections...

\[POOL/TTS] ElevenLabs connection warmed (HTTP 401)

\[POOL/STT] 2/2 connections ready

\[GATE] Concurrency gate ready — max 5 concurrent calls

\[MAIN] Waiting for Twilio calls...

\[TWIML] /voice hit  -> wss://violate-partner-lurk.ngrok-free.dev/  call\_sid=CA48fad92ec5a3bcc89dca54d19b5aa4ea

\[WS] Twilio Media Stream connected

\[ORCH] Waiting for stream SID from Twilio...

\[TWILIO] Stream started  SID=MZ8cd93c7a288191e9c91cc3231bd026da

\[ORCH] Governance ENABLED — language gate + restricted-topic filter active

\[RAG] LOCAL\_TEST mode — using mock embeddings (\[1.0]\*1536)

\[ORCH] RAG ENABLED — KnowledgeBase (pgvector) active

\[ORCH] Playing greeting...

\[ORCH] Pipeline active — call in progress

\[POOL/STT] Acquired pre-warmed connection (idle 11781ms)

\[STT] Connected to Deepgram STT (Nova-3)

\[STT] Speech recognised -> barge-in ('Ok, let me see')

\[STT] Interim: 'Ok, let me see'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('Ok,')

\[STT] Segment: 'Ok,'

\[STT] Interim: 'which is the best'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('which is the best courses of y')

\[STT] Interim: 'which is the best courses of your'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('which is the best courses of y')

\[STT] Final (speech\_final): 'Ok, which is the best courses of your college?'  \[lang=hi]

\[LLM] User said: 'Ok, which is the best courses of your college?'

\[RAG] Injecting context (score=0.74, cat=Admissions)

\[TTS] Sentence: 'We offer a variety of programs, but I'd be happy to help you...'

\[TTS] Sentence: 'Can you tell me what area of beauty and cosmetology interest...'

\[STT] Speech recognised -> barge-in ('Ok, máscara')

\[STT] Interim: 'Ok, máscara'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('Ok, माकार CBD.')

\[STT] Interim: 'Ok, माकार CBD.'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Final (speech\_final): 'Ok, माकार CBD.'  \[lang=hi]

\[LLM] User said: 'Ok, माकार CBD.'

\[RAG] Injecting context (score=0.71, cat=Student FAQs)

\[TTS] Sentence: 'I'm not sure I understand your question.'

\[TTS] Sentence: 'Could you please rephrase or ask about one of our programs, ...'

\[STT] Speech recognised -> barge-in ('Más')

\[STT] Interim: 'Más'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('Massage therapy.')

\[STT] Interim: 'Massage therapy.'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Interim: 'Massage therapy.'

\[STT] Speech recognised -> barge-in ('Massage therapy.')

\[STT] Segment: 'Massage therapy.'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Final (UtteranceEnd): 'Massage therapy.'  \[lang=en]

\[LLM] User said: 'Massage therapy.'

\[RAG] Injecting context (score=0.74, cat=Academic)

\[TTS] Sentence: 'Our Massage Therapy Diploma is a 2-year program that teaches...'

\[TTS] Sentence: 'The next batch starts on May 18, 2026. Would you like to kno...'

\[STT] Speech recognised -> barge-in ('No, thanks.')

\[STT] Interim: 'No, thanks.'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Interim: 'No, thank you'

\[STT] Speech recognised -> barge-in ('No, thank you.')

\[STT] Final (speech\_final): 'No, thank you.'  \[lang=en]

\[LLM] User said: 'No, thank you.'

\[RAG] Injecting context (score=0.71, cat=Admissions)

\[TTS] Sentence: 'You're welcome to reach out to us if you have any further qu...'

\[TTS] Sentence: 'Our campus is located at #108, 1935- 27 ave NE, Calgary, AB ...'

\[STT] Speech recognised -> barge-in ('Still,')

\[STT] Interim: 'Still,'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('Sure. Thanks.')

\[STT] Interim: 'Sure. Thanks.'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('Sure. Thanks.')

\[STT] Final (speech\_final): 'Sure. Thanks.'  \[lang=en]

\[LLM] User said: 'Sure. Thanks.'

\[RAG] Injecting context (score=0.71, cat=General Info)

\[TTS] Sentence: 'You're welcome.'

\[TTS] Sentence: 'It was nice speaking with you.'

\[TTS] Sentence: 'Have a great day and I hope you consider GD College for your...'

\[TTS] Sentence: 'Goodbye.'

\[TWILIO] Stream stopped

\[STATUS] CallSid=CA48fad92ec5a3bcc89dca54d19b5aa4ea  status=completed  duration=110s  ts=1782468229

\[STT] Disconnected from Deepgram STT

\[LLM] Shutting down

\[GATE] Released slot for CA48fad92ec5a3bcc89dca54d19b5aa4ea (completed), active=0

\[TTS] Shutting down

\[LOG] Transcript saved -> C:\\Users\\rmxhi\\Desktop\\Projects\\Ai VOICE agent\\logs\\transcripts\\2026-06-26\_10-01-59.json

\[LOG] Call summary saved -> C:\\Users\\rmxhi\\Desktop\\Projects\\Ai VOICE agent\\logs\\calls\\2026-06-26\_10-01-59\_MZ8cd93c7a288191e9c91cc3231bd026.json

\[ORCH] Call ended — all tasks complete





### test 7,8:at last its taking time to ans back and sometimes not responding or what is the issue...

### 

PS C:\\Users\\rmxhi\\Desktop\\Projects\\Ai VOICE agent> uv run run\_server.py

\[BOOT] LLM provider = Groq (llama-3.1-8b-instant)

\[MAIN] Starting modular voice pipeline server on port 5000

&#x20;      STT -> Deepgram nova-3

&#x20;      LLM -> Groq     llama-3.1-8b-instant

&#x20;      TTS -> ElevenLabs eleven\_flash\_v2\_5

&#x20;      Gate -> Redis    max 5 concurrent calls

\[MAIN] Warming up connection pools...

\[POOL/STT] Pre-warming 2 Deepgram connections...

\[POOL/TTS] ElevenLabs connection warmed (HTTP 401)

\[POOL/STT] 2/2 connections ready

\[GATE] Concurrency gate ready — max 5 concurrent calls

\[MAIN] Waiting for Twilio calls...

\[TWIML] /voice hit  -> wss://violate-partner-lurk.ngrok-free.dev/  call\_sid=CAbac66dbe2b1b81816e244cb86f36a944

\[WS] Twilio Media Stream connected

\[ORCH] Waiting for stream SID from Twilio...

\[TWILIO] Stream started  SID=MZd64b3b66c587d58e44f8025f8ddd6490

\[ORCH] Governance ENABLED — language gate + restricted-topic filter active

\[RAG] LOCAL\_TEST mode — using mock embeddings (\[1.0]\*1536)

\[ORCH] RAG ENABLED — KnowledgeBase (pgvector) active

\[ORCH] Playing greeting...

\[ORCH] Pipeline active — call in progress

\[POOL/STT] Acquired pre-warmed connection (idle 20250ms)

\[STT] Connected to Deepgram STT (Nova-3)

\[STT] Speech recognised -> barge-in ('Hello I')

\[STT] Interim: 'Hello I'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('Hello, I wanted you to')

\[STT] Interim: 'Hello, I wanted you to'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('Hello, I wanted you to')

\[STT] Final (speech\_final): 'Hello, I wanted you to'  \[lang=en]

\[LLM] User said: 'Hello, I wanted you to'

\[RAG] Injecting context (score=0.72, cat=Admissions)

\[TTS] Sentence: 'Hello, how can I assist you today?'

\[STT] Speech recognised -> barge-in ('I want a refund.')

\[STT] Final (speech\_final): 'I want a refund.'  \[lang=en]

\[LLM] User said: 'I want a refund.'

\[GOV-TOPIC] financial\_dispute -> canned refusal

\[TTS] Sentence: 'I cannot assist with fee disputes or refund policies over th...'

\[STT] Speech recognised -> barge-in ('Ok, tell me how to')

\[STT] Interim: 'Ok, tell me how to'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('Okay. Tell me about your meanw')

\[STT] Interim: 'Okay. Tell me about your meanwhile requirement.'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Segment: 'Ok, tell me about your meanwhile requirements.'

\[STT] Final (UtteranceEnd): 'Ok, tell me about your meanwhile requirements.'  \[lang=en]

\[LLM] User said: 'Ok, tell me about your meanwhile requirements.'

\[RAG] Injecting context (score=0.73, cat=Admissions)

\[TTS] Sentence: 'I don't have information on "meanwhile requirements" on hand...'

\[TTS] Sentence: 'If you can give me your full name and phone number, a colleg...'

\[STT] Speech recognised -> barge-in ('I mean,')

\[STT] Interim: 'I mean,'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('I mean, tell me about the')

\[STT] Interim: 'I mean, tell me about the'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('I meant tell me about the visa')

\[STT] Segment: 'I meant tell me about the visa requirement.'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Final (UtteranceEnd): 'I meant tell me about the visa requirement.'  \[lang=en]

\[LLM] User said: 'I meant tell me about the visa requirement.'

\[GOV-TOPIC] immigration -> canned refusal

\[TTS] Sentence: 'I can't help with immigration questions — please contact IRC...'

\[STT] Speech recognised -> barge-in ('When does the')

\[STT] Interim: 'When does the'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('When does the name technician')

\[STT] Interim: 'When does the name technician'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('When does the name sequential ')

\[STT] Final (speech\_final): 'When does the name sequential fetch start?'  \[lang=en]

\[LLM] User said: 'When does the name sequential fetch start?'

\[RAG] Injecting context (score=0.73, cat=Academic)

\[TTS] Sentence: 'I don't have information on "sequential fetch start" on hand...'

\[TTS] Sentence: 'If you can give me your full name and phone number, a colleg...'

\[STT] Speech recognised -> barge-in ('Ok,')

\[STT] Interim: 'Ok,'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('Okay. So when does the')

\[STT] Interim: 'Okay. So when does the'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('Okay. So when does the mail ta')

\[STT] Interim: 'Okay. So when does the mail take'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('Okay. So when does the nail te')

\[STT] Final (speech\_final): 'Okay. So when does the nail tech batch start?'  \[lang=en]

\[LLM] User said: 'Okay. So when does the nail tech batch start?'

\[RAG] Injecting context (score=0.75, cat=Academic)

\[TTS] Turn flush: 'The next batch for the Nail Technician Diploma starts on Feb'

\[STT] Speech recognised -> barge-in ('Good day.')

\[STT] Interim: 'Good day.'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('Could you tell me on')

\[STT] Interim: 'Could you tell me on'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('Could you tell me on your batc')

\[STT] Interim: 'Could you tell me on your batch course'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('Could you tell me on your batc')

\[STT] Final (speech\_final): 'Could you tell me on your batch courses?'  \[lang=en]

\[LLM] User said: 'Could you tell me on your batch courses?'

\[RAG] Injecting context (score=0.75, cat=Academic)

\[TTS] Sentence: 'We offer flexible schedules including morning, afternoon, ev...'

\[STT] Speech recognised -> barge-in ('Starting')

\[STT] Interim: 'Starting'

\[TTS] Barge-in mid-synthesis -> aborting stream

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('Starting time?')

\[STT] Interim: 'Starting time?'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('Starting time?')

\[STT] Final (speech\_final): 'Starting time?'  \[lang=en]

\[LLM] User said: 'Starting time?'

\[RAG] Injecting context (score=0.72, cat=Academic)

\[TTS] Sentence: 'We offer flexible schedules including morning, afternoon, ev...'

\[TTS] Sentence: 'However, I don't have specific start times for each program ...'

\[TTS] Sentence: 'If you can give me your full name and phone number, a colleg...'

\[STT] Speech recognised -> barge-in ('Mindfulness')

\[STT] Interim: 'Mindfulness'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('My full name is राहुल')

\[STT] Interim: 'My full name is राहुल'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('My full name is राहुल मुद्दा, ')

\[STT] Interim: 'My full name is राहुल मुद्दा, r a'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('My full name is राहुल मुद्दा, ')

\[STT] Segment: 'My full name is राहुल मुद्दा, r a h u l,'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('n u l a')

\[STT] Interim: 'n u l a'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('n u l a')

\[STT] Final (speech\_final): 'My full name is राहुल मुद्दा, r a h u l, n u l a'  \[lang=hi]

\[LLM] User said: 'My full name is राहुल मुद्दा, r a h u l, n u l a'

\[RAG] Injecting context (score=0.72, cat=General Info)

\[TTS] Sentence: 'I didn't quite catch that.'

\[TTS] Sentence: 'Could you please repeat your full name and phone number, so ...'

\[STT] Speech recognised -> barge-in ('and my phone number')

\[STT] Interim: 'and my phone number'

\[TTS] Barge-in mid-synthesis -> aborting stream

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('and my phone number is nine')

\[STT] Interim: 'and my phone number is nine'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('and my phone number is nine on')

\[STT] Interim: 'and my phone number is nine one three'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('and my phone number is nine on')

\[STT] Interim: 'and my phone number is nine one three five six seven'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('and my phone number is nine on')

\[STT] Interim: 'and my phone number is nine one three five six seven eight nine four'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Segment: 'and my phone number is nine one three five six seven eight nine four five.'

\[STT] Final (UtteranceEnd): 'and my phone number is nine one three five six seven eight nine four five.'  \[lang=en]

\[LLM] User said: 'and my phone number is nine one three five six seven eight nine four five.'

\[RAG] Injecting context (score=0.73, cat=Academic)

\[TTS] Sentence: 'I didn't quite catch that.'

\[TTS] Sentence: 'Could you please repeat your full name and phone number agai...'

\[TTS] Sentence: 'I'd like to make sure I get it right.'

\[STT] Speech recognised -> barge-in ('Ok.')

\[STT] Interim: 'Ok.'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('Ok, my name is')

\[STT] Interim: 'Ok, my name is'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('Well, my name is राहुल मूला.')

\[STT] Interim: 'Well, my name is राहुल मूला.'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('Ok, my name is राहुल मूला')

\[STT] Final (speech\_final): 'Ok, my name is राहुल मूला'  \[lang=hi]

\[LLM] User said: 'Ok, my name is राहुल मूला'

\[RAG] Injecting context (score=0.71, cat=General Info)

\[STT] Speech recognised -> barge-in ('and')

\[STT] Interim: 'and'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('and my phone number')

\[STT] Interim: 'and my phone number'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('and my phone number is')

\[STT] Interim: 'and my phone number is'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('and my phone number is nine on')

\[STT] Interim: 'and my phone number is nine one three'

\[LLM] Barge-in mid-generation -> abandoning stream

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('and my phone number is nine on')

\[STT] Interim: 'and my phone number is nine one three five six'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Segment: 'and my phone number is nine one three five'

\[STT] Speech recognised -> barge-in ('six five seven')

\[STT] Interim: 'six five seven'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('six five seven five eight five')

\[STT] Interim: 'six five seven five eight five.'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Final (speech\_final): 'and my phone number is nine one three five six five seven five eight five.'  \[lang=en]

\[LLM] User said: 'and my phone number is nine one three five six five seven five eight five.'

\[RAG] Injecting context (score=0.73, cat=Academic)

\[STT] Speech recognised -> barge-in ('Did you')

\[STT] Interim: 'Did you'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('Did you get my details?')

\[STT] Interim: 'Did you get my details?'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('Did you get my details?')

\[STT] Final (speech\_final): 'Did you get my details?'  \[lang=en]

\[LLM] User said: 'Did you get my details?'

\[TTS] Sentence: 'I've confirmed your full name as राहुल मूला and phone number...'

\[RAG] Injecting context (score=0.72, cat=Admissions)

\[STT] Speech recognised -> barge-in ('Alright. And now')

\[STT] Interim: 'Alright. And now'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('Alright. And')

\[STT] Final (speech\_final): 'Alright. And'  \[lang=en]

\[STT] Speech recognised -> barge-in ('Alright.')

\[STT] Interim: 'Alright.'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('Alright, Hannah.')

\[STT] Interim: 'Alright, Hannah.'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Final (speech\_final): 'Alright, Hannah.'  \[lang=en]

\[LLM] User said: 'Alright. And'

\[TTS] Sentence: 'Yes, I confirmed your full name as राहुल मूला and phone numb...'

\[RAG] Injecting context (score=0.71, cat=Fees)

\[STT] Speech recognised -> barge-in ('I am')

\[STT] Interim: 'I am'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('I am done.')

\[STT] Final (speech\_final): 'I am done.'  \[lang=en]

\[STT] 👋 Hangup phrase detected — call will end after farewell

\[LLM] User said: 'Alright, Hannah.'

\[RAG] Injecting context (score=0.71, cat=Student FAQs)

\[TTS] Sentence: 'We're running low on time, and our automated session is rest...'

\[TTS] Sentence: 'Is there anything else I can assist you with before I wrap u...'

\[STT] Speech recognised -> barge-in ('I am done.')

\[STT] Interim: 'I am done.'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('I am done.')

\[STT] Interim: 'I am done.'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Interim: 'I am done.'

\[STT] Speech recognised -> barge-in ('I am done.')

\[STT] Final (speech\_final): 'I am done.'  \[lang=en]

\[STT] 👋 Hangup phrase detected — call will end after farewell

\[STT] Speech recognised -> barge-in ('Alright. And')

\[STT] Interim: 'Alright. And'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('Alright. Hang on.')

\[STT] Final (speech\_final): 'Alright. Hang on.'  \[lang=en]

\[STT] Speech recognised -> barge-in ('Goodbye.')

\[STT] Interim: 'Goodbye.'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('Goodbye.')

\[STT] Final (speech\_final): 'Goodbye.'  \[lang=en]

\[STT] 👋 Hangup phrase detected — call will end after farewell

\[TTS] Sentence: 'I didn't quite catch that.'

\[LLM] User said: 'I am done.'

\[RAG] Injecting context (score=0.71, cat=General Info)

\[TTS] Sentence: 'I'm not aware of any previous conversation or confirmation o...'

\[TTS] Sentence: 'Our automated session is also running low on time, and I nee...'

\[TTS] Sentence: 'Is there anything else I can assist you with before I go?'

\[STT] Speech recognised -> barge-in ('And')

\[STT] Interim: 'And'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('Thanks. Bye.')

\[STT] Interim: 'Thanks. Bye.'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Speech recognised -> barge-in ('Thanks. Bye.')

\[STT] Final (speech\_final): 'Thanks. Bye.'  \[lang=en]

\[STT] 👋 Hangup phrase detected — call will end after farewell

\[STT] Interim: 'Va'

\[STT] Speech recognised -> barge-in ('Bye bye.')

\[STT] Interim: 'Bye bye.'

\[TTS] Barge-in → clearing buffer and Twilio audio queue

\[STT] Final (speech\_final): 'Bye bye.'  \[lang=en]

\[STT] 👋 Hangup phrase detected — call will end after farewell

\[LLM] Shutting down

\[TTS] Sentence: 'Thank you for reaching out to GD College, and I wish you the...'

\[TTS] Shutting down

\[TWILIO] Final mark received — playback drained

\[STT] Disconnected from Deepgram STT

\[STATUS] CallSid=CAbac66dbe2b1b81816e244cb86f36a944  status=completed  duration=329s  ts=1782468704

\[GATE] Released slot for CAbac66dbe2b1b81816e244cb86f36a944 (completed), active=0

\[LOG] Transcript saved -> C:\\Users\\rmxhi\\Desktop\\Projects\\Ai VOICE agent\\logs\\transcripts\\2026-06-26\_10-06-14.json

\[LOG] Call summary saved -> C:\\Users\\rmxhi\\Desktop\\Projects\\Ai VOICE agent\\logs\\calls\\2026-06-26\_10-06-14\_MZd64b3b66c587d58e44f8025f8ddd64.json

\[ORCH] Call ended — all tasks complete




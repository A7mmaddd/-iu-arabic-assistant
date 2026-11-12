# Class Diagram → Code  |  رسمة الكلاسات ← الكود

## models.py
| Class | Line | Methods (line) |
|---|---|---|
| User | 11 | loginWithGoogle (15), logOut (16) |
| Student (extends User) | 18 | uploadFile (22), requestSummary (26), requestTranslation (27), askTutor (28), askByVoice (36), listenTo (39), requestQuiz (41), takeExam (42) |
| Instructor (extends User) | 44 | uploadFile (48), generateExam (52), reviewQuestion (54), editQuestion (55), deleteQuestion (59), addOwnQuestion (60), publishExam (64), printExam (65), copyExam (66), viewResults (67) |
| ChatMessage | 69 | quotedText (73), thread (76) |
| Page | 86 | attributes only |
| CourseFile | 91 | validate (101), encrypt (102), getPages (103) |
| Summary | 105 | attributes only |
| Translation | 110 | attributes only |
| MCQQuestion | 114 | checkAnswer (118) |
| ExamQuestion (extends MCQQuestion) | 121 | clean (129), edit (139) |
| Quiz | 143 | submitAnswers (147) |
| Exam | 150 | addQuestion (156), removeQuestion (157), publish (158), attemptOf (161), startAttempt (162), submitAttempt (167), mostMissedQuestions (177), exportText (181) |
| ExamAttempt | 198 | recordTabLeave (204) |

## services.py
| Class | Line | Methods (line) |
|---|---|---|
| AuthService | 11 | checkProfile (34), authenticateWithGoogle (63), signIn (79), validateSession (85) |
| AIService (interface, ABC) | 87 | processFile (89), summarize (91), translatePart (93), translateSummary (95), answer (97), generateQuiz (99), generateExam (101), verifyQuestion (103) |
| LLMService (extends AIService) — uses Google Gemini (several models, a busy one is skipped) | 105 | processFile (192), summarize (241, splits big files into parts + coverage warning), translatePart (294), translateSummary (298), answer (303), generateQuiz (341), generateExam (347), verifyQuestion (360) |

## store.py
| Class | Methods |
|---|---|
| Store (Firestore, memory fallback) | profile, save_profile, claim_id, load_pages, save_pages, user_files, add_user_file, remove_user_file, chat, add_chat, cached, cache |

## static/index.html
| Class | Where | Methods |
|---|---|---|
| SpeechService «Service» | `const SpeechService` (line 827) | speechToText, textToSpeech (+ stopListening, stopSpeaking) — uses the free Web Speech API in the browser, so no server or API key is needed |

## app.py (web routes that use the classes)
| Use case | Route | Code used |
|---|---|---|
| Register / Login with Google | GET /api/config, POST /api/login `{credential}` | AuthService.authenticateWithGoogle (ALLOWED_EMAIL_DOMAIN) |
| **NEW** First-login profile (role from number) | POST /api/profile `{name, universityId, universityEmail, university}` | AuthService.checkProfile / roleFromId / signIn, Store.save_profile / claim_id |
| Upload Course File | POST /api/files | User.uploadFile → CourseFile.validate, LLMService.processFile → CourseFile.encrypt |
| Summarize Content | POST /api/files/<id>/summary (max 15 pages per call; the page sends bigger ranges in parts) | LLMService.summarize |
| **NEW** Delete Uploaded File | DELETE /api/files/<id> | removes the CourseFile from the user's list and their chat for it |
| **NEW** Reuse saved results | POST summary / translate | store.Store.cached / cache (by file SHA-256) |
| **NEW** Private tutor chat history | GET /api/files/<id>/chat | store.Store.chat / add_chat (per user) |
| Translate Summary | POST /api/summaries/<id>/translate | LLMService.translateSummary |
| Translate Selected Part | POST /api/files/<id>/translate | LLMService.translatePart |
| Ask AI Tutor (+ reject unrelated) | POST /api/files/<id>/ask | Student.askTutor → LLMService.answer, NOT_RELATED check |
| **NEW** Reply to an AI Answer | POST /api/files/<id>/ask with `replyTo` | ChatMessage.replyTo, ChatMessage.thread → LLMService.answer(history) |
| **NEW** Ask by Voice | 🎤 button → POST /ask with `voice: true` | SpeechService.speechToText (browser) → Student.askByVoice |
| **NEW** Listen to Answer | 🔊 "Listen" button | Student.listenTo → SpeechService.textToSpeech (browser) |
| Take Quiz | POST /api/files/<id>/quiz, /api/quizzes/<id>/submit | generateQuiz, Quiz.submitAnswers, MCQQuestion.checkAnswer |
| Generate Exam Questions **+ Choose Exam Language** | POST /api/files/<id>/exam `{n, language}` | Instructor.generateExam → LLMService.generateExam(f, n, language) |
| **NEW** Add Own Question «include» Verify Answer | POST /api/exams/<id>/questions | Instructor.addOwnQuestion → LLMService.verifyQuestion |
| Edit Question **& Options** | PUT /api/exams/<id>/questions/<i> | Instructor.editQuestion → ExamQuestion.edit (+ re-check with verifyQuestion) |
| Delete Question | DELETE /api/exams/<id>/questions/<i> | Instructor.deleteQuestion → Exam.removeQuestion |
| Edit / Delete only while not published | same routes | return 409 if Exam.isPublished |
| Publish Exam «include» Code + Lock | POST /api/exams/<id>/publish | Instructor.publishExam → Exam.publish |
| **NEW** Print / Copy Exam | GET /api/exams/<id>/export?for=print\|copy&answers=0\|1 | Instructor.printExam / copyExam → Exam.exportText |
| View Exam Results | GET /api/exams/<id>/results | Instructor.viewResults → ExamAttempt.to_dict |
| **NEW** Show Most-Missed Questions | same route (`mostMissed`) | Exam.mostMissedQuestions |
| **NEW** Show Tab-Leave Count | same route (`tabLeaves`) | ExamAttempt.tabLeaveCount |
| Take Published Exam «include» Validate Code & Single Attempt | POST /api/published/join | Exam.attemptOf, Exam.startAttempt |
| **NEW** Exam Mode: block other features | summary / translate / ask / quiz routes return 423 | open ExamAttempt (submittedAt is None) |
| **NEW** Exam Mode: track tab leaves | POST /api/published/<code>/leave | ExamAttempt.recordTabLeave |
| Exam Mode: come back after refresh | GET /api/published/active | open ExamAttempt |
| Submit exam «include» Show Correct Answer | POST /api/published/<code>/submit | Student.takeExam → Exam.submitAttempt → MCQQuestion.checkAnswer |

## Notes
- Verifying a question is a **warning, not a block**: the instructor can still keep and publish it.
- Exam mode is locked on the **server** (423), not only in the page. Leaving the page is **detected and counted**, not prevented
  (nobody can stop a student from using a phone).
- Voice input works in Chrome and Edge. Reading aloud works in all modern browsers; Arabic needs an Arabic voice on the device.
- Data is in memory only: files, exams and results are lost when the server restarts.

# Class Diagram → Code  |  رسمة الكلاسات ← الكود

## models.py
| Class | Line | Methods (line) |
|---|---|---|
| User | 11 | loginWithGoogle (15), logOut (16) |
| Student (extends User) | 18 | uploadFile (20), requestSummary (24), requestTranslation (25), askTutor (26), requestQuiz (27), takeExam (28) |
| Instructor (extends User) | 30 | uploadFile (34), generateExam (38), reviewQuestion (40), editQuestion (41), deleteQuestion (42), publishExam (43) |
| Page | 45 | attributes only |
| CourseFile | 50 | validate (60), encrypt (61), getPages (62) |
| Summary | 64 | attributes only |
| Translation | 69 | attributes only |
| MCQQuestion | 73 | checkAnswer (77) |
| ExamQuestion (extends MCQQuestion) | 80 | edit (87) |
| Quiz | 89 | submitAnswers (93) |
| Exam | 96 | addQuestion (101), removeQuestion (102), publish (103), submitAttempt (107) |
| ExamAttempt | 114 | attributes only |

## services.py
| Class | Line | Methods (line) |
|---|---|---|
| AuthService | 6 | authenticateWithGoogle (8), validateSession (20) |
| AIService (interface, ABC) | 22 | processFile (24), summarize (26), translatePart (28), translateSummary (30), answer (32), generateQuiz (34), generateExam (36) |
| LLMService (implements AIService) | 38 | processFile (64), summarize (75), translatePart (82), translateSummary (86), answer (90), generateQuiz (100), generateExam (105) |

## app.py (web routes that use the classes)
| Use case | Route | Code used |
|---|---|---|
| Register / Login with Google | POST /api/login | AuthService.authenticateWithGoogle |
| Upload Course File | POST /api/files | User.uploadFile → CourseFile.validate, LLMService.processFile → CourseFile.encrypt |
| Summarize Content | POST /api/files/<id>/summary | LLMService.summarize |
| Translate Summary | POST /api/summaries/<id>/translate | LLMService.translateSummary |
| Translate Selected Part | POST /api/files/<id>/translate | LLMService.translatePart |
| Ask AI Tutor (+ reject unrelated) | POST /api/files/<id>/ask | LLMService.answer, NOT_RELATED check |
| Take Quiz | POST /api/files/<id>/quiz, /api/quizzes/<id>/submit | generateQuiz, Quiz.submitAnswers, MCQQuestion.checkAnswer |
| Generate Exam | POST /api/files/<id>/exam | Instructor.generateExam → LLMService.generateExam |
| Edit / Delete Question | PUT / DELETE /api/exams/<id>/questions/<i> | ExamQuestion.edit, Exam.removeQuestion |
| Edit / Delete Question (locked once published) | PUT / DELETE /api/exams/<id>/questions/<i> | returns 409 if Exam.isPublished |
| **NEW** List / Open my exams | GET /api/exams, GET /api/exams/<id> | Instructor.exams |
| **NEW** Publish Exam | POST /api/exams/<id>/publish | Instructor.publishExam → Exam.publish (join code) |
| **NEW** View Exam Results | GET /api/exams/<id>/results | Exam.attempts → ExamAttempt.to_dict |
| **NEW** Take Published Exam | POST /api/published/join, POST /api/published/<code>/submit | Student.takeExam → Exam.submitAttempt → MCQQuestion.checkAnswer |

## Exam publishing — changes to the class diagram
- Exam: + title: str, + isPublished: bool, + code: str, + attempts: List<ExamAttempt>; + publish(): str, + attemptOf(email), + submitAttempt(student, answers)
- Instructor: + publishExam(exam): str
- Student: + takeExam(exam, answers): ExamAttempt
- New class ExamAttempt (studentName, email, answers, score, total, submittedAt); Exam 1 --> 0..* ExamAttempt; Student 1 --takes--> 0..* ExamAttempt
- New use cases: Publish Exam (Instructor), View Exam Results (Instructor), Take Published Exam (Student, «include» Show Correct Answer & Explanation)
- Data is in memory only: published exams and results are lost when the server restarts.

import type { UseCase, CRMProfile, PastInteraction, Scenario, LatencyBreakdown } from '../types';

// Call-only mock data + helpers for the sandbox call view (roadmap Q-1).
// Consumed by useLiveCall (scenarios, CRM, filler, latency) and by
// ActiveCallView (PAST_INTERACTIONS in the customer-context panel).

export const MOCK_CUSTOMERS: Record<UseCase, CRMProfile> = {
  'Credit Card': {
    name: 'Rahul Sharma', customerId: 'CUST-882910', phone: '+91 98*** ***21', segment: 'High Net Worth',
    fields: [
      { label: 'Pre-Approved Limit', value: '₹5,00,000', highlight: 'green' },
      { label: 'Eligible Offer', value: 'Regalia Gold Card', highlight: 'green' },
      { label: 'Offer Type', value: 'Pre-Approved · FYF' },
      { label: 'Credit Score', value: '810' },
      { label: 'Recent Activity', value: 'Web login 2 days ago' },
    ]
  },
  'Collections': {
    name: 'Amit Patel', customerId: 'CUST-554102', phone: '+91 99*** ***44', segment: 'Salaried',
    fields: [
      { label: 'Amount Due', value: '₹ 12,500', highlight: 'orange' },
      { label: 'DPD', value: '0 (Pre-Due)' },
      { label: 'Due Date', value: '15 Jun 2026' },
      { label: 'Payment History', value: '11/12 on time' },
    ]
  },
  'Insurance': {
    name: 'Vikram Desai', customerId: 'CUST-119283', phone: '+91 76*** ***90', segment: 'Self-Employed',
    fields: [
      { label: 'Product Interest', value: 'Term Plan ₹1 Cr', highlight: 'green' },
      { label: 'Quoted Premium', value: '₹ 1,450 / month' },
      { label: 'Age Band', value: '32–35' },
      { label: 'Recent Activity', value: 'Premium calculator, app' },
    ]
  },
  'Personal Loan': {
    name: 'Sneha Iyer', customerId: 'CUST-773645', phone: '+91 88*** ***07', segment: 'Pre-Screened',
    fields: [
      { label: 'Pre-Approved Amount', value: '₹ 4,00,000', highlight: 'green' },
      { label: 'Indicative ROI', value: '10.75% p.a.' },
      { label: 'Max Tenure', value: '60 months' },
      { label: 'FOIR Headroom', value: '38%' },
    ]
  },
};

export const PAST_INTERACTIONS: PastInteraction[] = [
  {
    id: 'pi-1', date: '12 May 2026', topic: 'KYC Update Request', duration: '1m 20s',
    transcript: [
      { speaker: 'bot', text: 'Namaste Rahul ji, aapka KYC pending hai. Kya online update kar lenge?' },
      { speaker: 'user', text: 'Haan, main aaj shaam tak app se kar dunga.' },
      { speaker: 'bot', text: 'Dhanyawad. Main aapko SMS pe link bhej rahi hoon.' }
    ]
  },
  {
    id: 'pi-2', date: '03 April 2026', topic: 'Welcome Call', duration: '2m 10s',
    transcript: [
      { speaker: 'bot', text: 'Welcome to HDFC Bank, Rahul ji. Aapki account details mail kar di gayi hain.' },
      { speaker: 'user', text: 'Thank you. Debit card kab aayega?' },
      { speaker: 'bot', text: 'Sir, debit card 7 working days mein aapke registered address par deliver ho jayega.' }
    ]
  }
];

// Item 17: Insurance consent capture added to "Send me the quote" chip
export const SCENARIOS: Record<UseCase, Scenario> = {
  'Credit Card': {
    greeting: 'Namaste Rahul ji, main HDFC Bank ki taraf se KOEL bol rahi hoon — yeh ek automated call hai. Aapke account par ek pre-approved Regalia credit card offer hai. Do minute baat kar sakte hain?',
    greetingTone: 'cheerful',
    rePrompt: 'Hello, aap sun pa rahe hain? Are you still there?',
    politeClose: 'Lagta hai aap busy hain. Main baad mein call karti hoon. Dhanyavaad.',
    bargeInUserText: 'Wait wait — pehle yeh batao isme charges kya hain?',
    bargeInReply: 'Bilkul sir. Pehle saal ki joining fee poori tarah waived hai, aur ₹3 lakh annual spend par renewal fee bhi waive ho jaati hai. Koi hidden charges nahi.',
    chips: [
      { label: '"I\'m busy right now"', userText: 'Abhi thoda busy hoon.', botReply: 'Koi baat nahi sir. Main aapko shaam ko 6 baje call kar leti hoon. Thank you.', tone: 'neutral', action: 'END_POLITE', outcome: { label: 'Callback Scheduled', status: 'gray' } },
      { label: '"I already have 2 cards"', userText: 'Haan boliye, but mere paas already do cards hain.', botReply: 'Bilkul samajh sakti hoon. Yeh card lounge access aur 4x reward points deta hai dining par — aapke existing cards se alag benefit. Pehle saal ki fee bhi waived hai. Interested hain?', tone: 'empathetic', ragContext: 'RAG: Objection Handling — Multiple Cards (hdfc_regalia_benefits_v2)', nlpSlow: true },
      { label: '"Talk to a human"', userText: 'Theek hai, thoda detail chahiye. Koi bande se baat kara do.', botReply: 'Zaroor. Main abhi aapko ek specialist se connect karti hoon jo poori detail denge. Ek second hold kijiye.', tone: 'cheerful', action: 'ESCALATE', outcome: { label: 'Escalated to Agent', status: 'orange' } },
    ]
  },
  'Collections': {
    greeting: 'Namaste Amit ji, main ICICI Bank ki taraf se KOEL bol rahi hoon — yeh ek automated reminder call hai. Aapke credit card ka ₹12,500 ka bill 15 June ko due hai. Kya aap payment plan confirm kar sakte hain?',
    greetingTone: 'firm',
    rePrompt: 'Hello, aap sun pa rahe hain? Are you still there?',
    politeClose: 'Lagta hai aap busy hain. Main baad mein call karti hoon. Dhanyavaad.',
    bargeInUserText: 'Ruko ruko — maine partial payment already kar di hai!',
    bargeInReply: 'Oh, dhanyavaad batane ke liye. Mujhe record mein ₹5,000 ki payment dikh rahi hai. Remaining ₹7,500 ke liye kya main aapko payment link bhej doon?',
    chips: [
      { label: '"I will pay tomorrow"', userText: 'Haan pata hai, main kal pay kar dunga.', botReply: 'Dhanyavaad confirm karne ke liye. Maine aapka promise-to-pay kal ke liye record kar liya hai. Convenience ke liye SMS par payment link bhi bhej rahi hoon. Have a great day!', tone: 'empathetic', ragContext: 'Action: PROMISE_TO_PAY recorded · SMS_PAYMENT_LINK_SENT', action: 'END_POLITE', outcome: { label: 'Promise to Pay', status: 'green' } },
      { label: '"Can I pay partially?"', userText: 'Poora nahi ho payega abhi. Aadha kar sakta hoon kya?', botReply: 'Ji bilkul. Aap minimum due ₹2,500 ya partial ₹6,000 abhi pay kar sakte hain — interest sirf remaining balance par lagega. Main link bhej doon?', tone: 'empathetic', ragContext: 'RAG: Partial Payment Policy (icici_collection_guidelines_2026)', nlpSlow: true },
      { label: '"This bill is wrong"', userText: 'Yeh bill galat hai, maine itna use hi nahi kiya.', botReply: 'Main samajh sakti hoon, billing dispute serious hai. Main aapko abhi dispute team se connect karti hoon — aapko dobara details repeat nahi karni padengi.', tone: 'firm', action: 'ESCALATE', outcome: { label: 'Dispute — Escalated', status: 'orange' } },
    ]
  },
  'Insurance': {
    greeting: 'Hello Vikram sir, this is KOEL calling from MaxLife — this is an automated call. You recently checked term insurance premiums on our app. Per IRDAI guidelines I must mention: insurance is subject to market and policy terms. May I share a quote that matches your profile?',
    greetingTone: 'cheerful',
    rePrompt: 'Hello, can you hear me? Are you still there?',
    politeClose: 'It seems you are busy. I will call back later. Thank you for your time.',
    bargeInUserText: 'Hold on — is the premium fixed or will it increase?',
    bargeInReply: 'Great question. The premium is locked for the entire policy term once issued — it never increases with age or claims. That is exactly why buying early saves money.',
    chips: [
      { label: '"Premium is too high"', userText: 'Yes, but the premium was a bit high.', botReply: 'I understand. If you opt for annual payments instead of monthly, you get a 5% discount. Plus, at your age, locking in this premium now saves you money long-term. Shall I send the revised quote on WhatsApp?', tone: 'empathetic', ragContext: 'RAG: Premium Objection Handling (maxlife_term_disclosures)', nlpSlow: true },
      { label: '"Send me the quote"', userText: 'Okay, send it on WhatsApp.', botReply: 'Done sir — the revised quote with the annual-payment discount is on its way to your WhatsApp. A licensed advisor will follow up for the final steps. Thank you!', tone: 'cheerful', ragContext: 'Action: WHATSAPP_QUOTE_SENT · Advisor callback booked', action: 'END_POLITE', outcome: { label: 'Completed — Interested', status: 'green' }, consentCapture: 'VERBAL_CONSENT captured · IRDAI compliance logged' },
      { label: '"Talk to an advisor"', userText: 'I would rather discuss this with a person.', botReply: 'Of course. I am connecting you to a licensed advisor right away — they will have this full conversation context. One moment please.', tone: 'neutral', action: 'ESCALATE', outcome: { label: 'Escalated to Advisor', status: 'orange' } },
    ]
  },
  'Personal Loan': {
    greeting: 'Namaste Sneha ji, main Axis Bank ki taraf se KOEL bol rahi hoon — yeh ek automated call hai. Aapke liye ₹4 lakh tak ka pre-approved personal loan offer hai, sirf 10.75% interest par. Kya do minute baat kar sakte hain?',
    greetingTone: 'cheerful',
    rePrompt: 'Hello, aap sun pa rahe hain? Are you still there?',
    politeClose: 'Lagta hai aap busy hain. Main baad mein call karti hoon. Dhanyavaad.',
    bargeInUserText: 'Ek minute — processing fee kitni hai iski?',
    bargeInReply: 'Sure ma\'am. Processing fee sirf 1% hai, aur is offer mein woh bhi 50% discounted hai. Koi prepayment penalty nahi hai 12 EMIs ke baad.',
    chips: [
      { label: '"What will my EMI be?"', userText: 'EMI kitni banegi 4 lakh par?', botReply: '₹4 lakh par 36 months ke liye EMI banegi lagbhag ₹13,050 per month. Agar tenure 60 months karein toh ₹8,650. Kaunsa option better lagta hai?', tone: 'neutral', ragContext: 'Dynamic EMI calculation: P=₹4,00,000 · r=10.75% · n=36/60', nlpSlow: true },
      { label: '"Book a callback"', userText: 'Theek hai, mujhe loan team se baat karni hai. Kal subah call karwa do.', botReply: 'Perfect ma\'am. Maine kal subah 10 baje loan team ka callback book kar diya hai. Aapko SMS confirmation mil jayega. Dhanyavaad!', tone: 'cheerful', ragContext: 'Action: CALLBACK_BOOKED — Loan team, 10:00 AM', action: 'END_POLITE', outcome: { label: 'Callback Booked', status: 'green' } },
      { label: '"Not interested"', userText: 'Nahi, mujhe abhi loan nahi chahiye.', botReply: 'Koi baat nahi ma\'am. Main aapki preference record kar leti hoon — aapko is offer ke liye dobara call nahi aayega. Apna din shubh ho!', tone: 'neutral', ragContext: 'CRM Flag: OPT_OUT honoured (TRAI compliance)', action: 'END_POLITE', outcome: { label: 'Declined — Opt Out', status: 'red' } },
    ]
  },
};

// Item 20: Fixed seeded history greetings — all include automated self-identification

export const FILLER_POOLS: Record<string, string[]> = {
  Hinglish: ['"Hmm… ek second rukiye…"', '"Ji, main abhi check karti hoon…"', '"Achha, bas ek moment dijiye…"', '"Ek minute, main dekh rahi hoon…"', '"Ji bilkul, abhi batati hoon…"', '"Haan ji, bas do second…"'],
  Hindi: ['"Hmm… ek second rukiye…"', '"Ji, main abhi dekh rahi hoon…"', '"Bas ek pal rukiye…"', '"Ek minute dijiye, abhi batati hoon…"', '"Ji, zaroor — bas ek second…"'],
  English: ['"Hmm… let me pull that up…"', '"One moment please…"', '"Right, just a second…"', '"Bear with me a moment…"', '"Let me just check that for you…"'],
};

export const randLatency = (slowNlp = false): LatencyBreakdown => {
  const stt = 55 + Math.floor(Math.random() * 35);
  const nlp = slowNlp ? 950 + Math.floor(Math.random() * 600) : 95 + Math.floor(Math.random() * 70);
  const tts = 60 + Math.floor(Math.random() * 35);
  return { stt, nlp, tts, total: stt + nlp + tts };
};

export const LANG_CODES: Record<string, string> = { Hinglish: 'hi-Latn', Hindi: 'hi', English: 'en' };

let msgId = 1000;
export const nextId = () => ++msgId;

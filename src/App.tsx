import React, { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import {
  getBots as apiGetBots,
  startSession as apiStartSession,
  createBot,
  patchBot,
  getKnowledgeIndexes,
  createKnowledgeChunk,
  uploadKnowledgeFile,
  getAllStats,
  refreshToken,
  saveToken,
  loadStoredToken,
  clearStoredToken,
  tokenExpiresInMs,
  USE_CASE_CUSTOMER,
  LiveCall,
  KoelApiError,
  type BackendBot,
  type NewBotPayload,
  type KnowledgeIndex,
  type BotStats,
} from './lib/koel';
import { MicCapture, micSupported } from './lib/audio';
import {
  Play, Square, Mic, Settings, Clock, PhoneCall, Plus,
  BarChart2, User, Database, Activity, ShieldAlert, Zap,
  MessageSquare, ChevronRight, ChevronDown, CheckCircle2, AlertCircle, X, Filter,
  Download, Search, LogOut, Lock, WifiOff, Languages, Timer, Volume2, Radio, Pause,
  Sun, Moon, Menu, Edit3, BookOpen, AlertTriangle
} from 'lucide-react';
import type {
  ViewState, CallState, Tone, UseCase, AmbientProfile, Bot, BotConfig,
  CRMProfile, LatencyBreakdown, TranscriptMessageData, CallHistory,
  PastInteraction, ChipDef, Scenario, SessionEvent,
} from './types';
import { getTheme } from './lib/theme';
import { LoginView } from './views/LoginView';

// Shared domain types now live in ./types; getTheme in ./lib/theme (Q-1 decomposition).


const TONE_STYLES: Record<Tone, string> = {
  cheerful: 'bg-[#FFF7E6] text-[#9A6700] border-[#F5D789]',
  empathetic: 'bg-[#F2EAFB] text-[#6B2FB3] border-[#D9C2F0]',
  firm: 'bg-[#FFF0EB] text-[#EB5324] border-[#F5C0AD]',
  neutral: 'bg-gray-100 text-gray-600 border-gray-300'
};

// ============================================================================
// Mock data — bots, CRM profiles, scenarios, seeded history
// ============================================================================
const now = Date.now();

// Named instances mirror the PRD §09 Screen 1 examples (HDFC Credit Card,
// ICICI Credit Card, Insurance Bot) while keeping all four use cases covered.
const INITIAL_BOTS: Bot[] = [
  { id: 'b1', name: 'HDFC Credit Card', useCase: 'Credit Card', language: 'Hinglish', status: 'Active', calls: 1240, conversionNum: 14.2, conversion: '14.2%', avgDuration: '1m 38s', escalationRate: '9.1%', lastActive: '2 min ago', lastActiveTs: now - 120000 },
  { id: 'b2', name: 'ICICI Credit Card', useCase: 'Credit Card', language: 'Hinglish', status: 'Active', calls: 2110, conversionNum: 12.7, conversion: '12.7%', avgDuration: '1m 51s', escalationRate: '8.4%', lastActive: '40 sec ago', lastActiveTs: now - 40000 },
  { id: 'b3', name: 'Insurance Bot', useCase: 'Insurance', language: 'English', status: 'Active', calls: 450, conversionNum: 8.4, conversion: '8.4%', avgDuration: '3m 12s', escalationRate: '18.6%', lastActive: '6 min ago', lastActiveTs: now - 360000 },
  { id: 'b4', name: 'ICICI Pre-Due Collections', useCase: 'Collections', language: 'Hindi', status: 'Active', calls: 8900, conversionNum: 32.1, conversion: '32.1%', avgDuration: '0m 54s', escalationRate: '4.3%', lastActive: '14 sec ago', lastActiveTs: now - 14000 },
  { id: 'b5', name: 'Axis Loan Origination', useCase: 'Personal Loan', language: 'Hinglish', status: 'Active', calls: 3200, conversionNum: 21.5, conversion: '21.5%', avgDuration: '2m 05s', escalationRate: '11.8%', lastActive: '1 hr ago', lastActiveTs: now - 3600000 },
];

// Item 33: offer amount added to Credit Card CRM
const MOCK_CUSTOMERS: Record<UseCase, CRMProfile> = {
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

const PAST_INTERACTIONS: PastInteraction[] = [
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
const SCENARIOS: Record<UseCase, Scenario> = {
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
const MOCK_HISTORY_CALLS: CallHistory[] = [
  {
    id: 'CALL-9921', bot: 'HDFC Card Cross-Sell', language: 'Hinglish', durationStr: '1m 45s', durationSec: 105, outcome: 'Escalated to Agent', dateStr: '09 Jun, 14:30', timestamp: 1781015400000, status: 'orange',
    customer: 'Rahul Sharma', customerId: 'CUST-882910', phone: '+91 98*** ***21', avgLatencyMs: 268,
    transcript: [
      { id: 1, kind: 'bot', tone: 'cheerful', latency: { stt: 0, nlp: 0, tts: 180, total: 180 }, text: 'Namaste Rahul ji, main HDFC Bank ki taraf se KOEL bol rahi hoon — yeh ek automated call hai. Aapke account par ek pre-approved Regalia credit card offer hai. Do minute baat kar sakte hain?' },
      { id: 2, kind: 'user', text: 'Haan boliye, but mere paas already do cards hain.' },
      { id: 3, kind: 'meta', text: 'RAG Retrieval: Objection Handling — Multiple Cards' },
      { id: 4, kind: 'bot', tone: 'empathetic', latency: { stt: 70, nlp: 130, tts: 85, total: 285 }, text: 'Bilkul samajh sakti hoon. Yeh card lounge access aur 4x reward points deta hai dining par — aapke existing cards se alag benefit. Pehle saal ki fee bhi waived hai. Interested hain?' },
      { id: 5, kind: 'user', text: 'Theek hai, thoda detail chahiye. Koi bande se baat kara do.' },
      { id: 6, kind: 'meta', text: 'Escalation triggered: HIGH_INTEREST · Full transcript + CRM context handed to agent queue.' }
    ]
  },
  {
    id: 'CALL-9920', bot: 'ICICI Pre-Due Collections', language: 'Hindi', durationStr: '0m 32s', durationSec: 32, outcome: 'Promise to Pay', dateStr: '09 Jun, 12:15', timestamp: 1781007300000, status: 'green',
    customer: 'Amit Patel', customerId: 'CUST-554102', phone: '+91 99*** ***44', avgLatencyMs: 241,
    transcript: [
      { id: 1, kind: 'bot', tone: 'firm', text: 'Namaste Amit ji, main ICICI Bank ki taraf se KOEL bol rahi hoon — yeh ek automated reminder call hai. Aapke credit card ka ₹12,500 ka bill 15 June ko due hai. Kya aap payment plan confirm kar sakte hain?' },
      { id: 2, kind: 'user', text: 'Yes, I know. I will pay it tomorrow.' },
      { id: 3, kind: 'bot', tone: 'empathetic', text: 'Thank you for confirming. I have recorded your promise to pay for tomorrow. I will also send a payment link via SMS for your convenience. Have a great day!' },
      { id: 4, kind: 'meta', text: 'Action: PROMISE_TO_PAY recorded · SMS_PAYMENT_LINK_SENT' }
    ]
  },
  {
    id: 'CALL-9919', bot: 'HDFC Card Cross-Sell', language: 'Hinglish', durationStr: '0m 12s', durationSec: 12, outcome: 'Customer Hung Up', dateStr: '09 Jun, 09:10', timestamp: 1780996200000, status: 'red',
    customer: 'Priya Singh', customerId: 'CUST-882910', phone: '+91 87*** ***19', avgLatencyMs: 255,
    transcript: [
      { id: 1, kind: 'bot', tone: 'cheerful', text: 'Namaste Priya ji, main HDFC Bank ki taraf se KOEL bol rahi hoon — yeh ek automated call hai. Aapke account par ek pre-approved...', interrupted: true },
      { id: 2, kind: 'user', text: 'Mujhe koi card nahi chahiye, please mat call karo.' },
      { id: 3, kind: 'meta', text: 'Opt-out intent detected · CRM flagged DO_NOT_CALL · Session disconnected by user.' }
    ]
  },
  {
    id: 'CALL-9918', bot: 'MaxLife Term Pitch', language: 'English', durationStr: '4m 10s', durationSec: 250, outcome: 'Completed — Interested', dateStr: '08 Jun, 16:45', timestamp: 1780937100000, status: 'green',
    customer: 'Vikram Desai', customerId: 'CUST-119283', phone: '+91 76*** ***90', avgLatencyMs: 289,
    transcript: [
      { id: 1, kind: 'bot', tone: 'cheerful', text: 'Hello Vikram sir, this is KOEL calling from MaxLife — this is an automated call. You recently checked term insurance premiums on our app...' },
      { id: 2, kind: 'user', text: 'Yes, but the premium was a bit high.' },
      { id: 3, kind: 'meta', text: 'RAG Retrieval: Premium Objection Handling' },
      { id: 4, kind: 'filler', text: '"Hmm, let me pull that up…"' },
      { id: 5, kind: 'bot', tone: 'empathetic', latency: { stt: 80, nlp: 1240, tts: 90, total: 1410 }, text: 'I understand. However, if you opt for annual payments instead of monthly, you get a 5% discount. Plus, at your age, locking in this premium now saves you money in the long run. Would you like me to send the revised quote?' },
      { id: 6, kind: 'user', text: 'Okay, send it on WhatsApp.' },
      { id: 7, kind: 'meta', text: 'Action: WHATSAPP_QUOTE_SENT · VERBAL_CONSENT captured' }
    ]
  },
  {
    id: 'CALL-9917', bot: 'ICICI Pre-Due Collections', language: 'Hindi', durationStr: '1m 05s', durationSec: 65, outcome: 'Wrong Number', dateStr: '08 Jun, 10:20', timestamp: 1780914000000, status: 'gray',
    customer: 'Unknown User', customerId: 'N/A', phone: '+91 90*** ***22', avgLatencyMs: 232,
    transcript: [
      { id: 1, kind: 'bot', tone: 'neutral', text: 'Hello, am I speaking with Mr. Suresh? Main ICICI Bank ki taraf se KOEL bol rahi hoon — yeh ek automated call hai.' },
      { id: 2, kind: 'user', text: 'Nahi bhai, wrong number hai.' },
      { id: 3, kind: 'bot', tone: 'empathetic', text: 'Oh, I apologize for the inconvenience. We will update our records. Have a good day.' },
      { id: 4, kind: 'meta', text: 'CRM Flag: UPDATE_WRONG_NUMBER' }
    ]
  },
  {
    id: 'CALL-9916', bot: 'Axis Loan Origination', language: 'Hinglish', durationStr: '0m 48s', durationSec: 48, outcome: 'Call Dropped', dateStr: '07 Jun, 18:05', timestamp: 1780855500000, status: 'gray',
    customer: 'Sneha Iyer', customerId: 'CUST-773645', phone: '+91 88*** ***07', avgLatencyMs: 274,
    transcript: [
      { id: 1, kind: 'bot', tone: 'cheerful', text: 'Namaste Sneha ji, main Axis Bank ki taraf se KOEL bol rahi hoon — yeh ek automated call hai. Aapke liye ₹4 lakh tak ka pre-approved personal loan offer hai...' },
      { id: 2, kind: 'meta', text: 'WebSocket disconnected · Redis session held on 15s grace TTL' },
      { id: 3, kind: 'meta', text: 'No reconnect within grace window · Outcome: call_dropped · CRM flagged for callback' }
    ]
  },
];

// ============================================================================
// Helpers
// ============================================================================
const formatTime = (seconds: number) => {
  const m = Math.floor(seconds / 60).toString().padStart(2, '0');
  const s = (seconds % 60).toString().padStart(2, '0');
  return `${m}:${s}`;
};

const formatRelativeTime = (ts: number) => {
  const diff = Math.max(0, Math.floor((Date.now() - ts) / 1000));
  if (diff < 60) return `${diff} sec ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} hr ago`;
  return `${Math.floor(diff / 86400)} days ago`;
};

const FILLER_POOLS: Record<string, string[]> = {
  Hinglish: ['"Hmm… ek second rukiye…"', '"Ji, main abhi check karti hoon…"', '"Achha, bas ek moment dijiye…"', '"Ek minute, main dekh rahi hoon…"', '"Ji bilkul, abhi batati hoon…"', '"Haan ji, bas do second…"'],
  Hindi: ['"Hmm… ek second rukiye…"', '"Ji, main abhi dekh rahi hoon…"', '"Bas ek pal rukiye…"', '"Ek minute dijiye, abhi batati hoon…"', '"Ji, zaroor — bas ek second…"'],
  English: ['"Hmm… let me pull that up…"', '"One moment please…"', '"Right, just a second…"', '"Bear with me a moment…"', '"Let me just check that for you…"'],
};

const randLatency = (slowNlp = false): LatencyBreakdown => {
  const stt = 55 + Math.floor(Math.random() * 35);
  const nlp = slowNlp ? 950 + Math.floor(Math.random() * 600) : 95 + Math.floor(Math.random() * 70);
  const tts = 60 + Math.floor(Math.random() * 35);
  return { stt, nlp, tts, total: stt + nlp + tts };
};

const LANG_CODES: Record<string, string> = { Hinglish: 'hi-Latn', Hindi: 'hi', English: 'en' };

let msgId = 1000;
const nextId = () => ++msgId;

// Default config per use case
const DEFAULT_CONFIGS: Record<UseCase, BotConfig> = {
  'Credit Card': { name: 'HDFC Card Cross-Sell', useCase: 'Credit Card Cross-Sell', language: 'Hinglish', persona: 'Kore — Female, Firm & Professional', speed: 1.0, ambientVolume: 20, ambientProfile: 'callfloor', backchannels: ['hmm', 'haan', 'okay', 'achha', 'theek hai', 'yeah'], ragEnabled: true, ragIndex: 'hdfc_regalia_benefits_v2', greeting: 'Namaste {customer.name} ji, main {bank} ki taraf se KOEL bol rahi hoon — yeh ek automated call hai. Aapke account par ek pre-approved {offer.product} offer hai. Do minute baat kar sakte hain?', tone: 'cheerful' },
  'Collections': { name: 'ICICI Pre-Due Collections', useCase: 'Collections — Pre-Due (DPD 0–30)', language: 'Hindi', persona: 'Kore — Female, Firm & Professional', speed: 1.0, ambientVolume: 20, ambientProfile: 'callfloor', backchannels: ['hmm', 'haan', 'okay', 'achha', 'theek hai', 'yeah'], ragEnabled: true, ragIndex: 'icici_collection_guidelines_2026', greeting: 'Namaste {customer.name} ji, main {bank} ki taraf se KOEL bol rahi hoon — yeh ek automated reminder call hai.', tone: 'firm' },
  'Insurance': { name: 'MaxLife Term Pitch', useCase: 'Life Insurance Sell (IRDAI flow)', language: 'English', persona: 'Zephyr — Female, Bright & Cheerful', speed: 1.0, ambientVolume: 20, ambientProfile: 'callfloor', backchannels: ['hmm', 'okay', 'yeah', 'right', 'I see'], ragEnabled: true, ragIndex: 'maxlife_term_disclosures', greeting: 'Hello {customer.name} sir, this is KOEL calling from MaxLife — this is an automated call.', tone: 'cheerful' },
  'Personal Loan': { name: 'Axis Loan Origination', useCase: 'Personal Loan Sell', language: 'Hinglish', persona: 'Zephyr — Female, Bright & Cheerful', speed: 1.0, ambientVolume: 20, ambientProfile: 'callfloor', backchannels: ['hmm', 'haan', 'okay', 'achha', 'theek hai', 'yeah'], ragEnabled: true, ragIndex: 'axis_pl_rate_card_q2', greeting: 'Namaste {customer.name} ji, main {bank} ki taraf se KOEL bol rahi hoon — yeh ek automated call hai.', tone: 'cheerful' },
};

// ============================================================================
// Root App — login gate + view router
// ============================================================================
// Map a backend use_case to the frontend UseCase label used by SCENARIOS/CRM.
const USE_CASE_LABEL: Record<string, UseCase> = {
  credit_card: 'Credit Card',
  collections: 'Collections',
  insurance: 'Insurance',
  personal_loan: 'Personal Loan',
};

// Map the frontend config form to the backend's bot-create payload (enum-safe).
function configToNewBot(config: BotConfig): NewBotPayload {
  const uc = `${config.useCase} ${config.name}`.toLowerCase();
  const use_case: NewBotPayload['use_case'] =
    /collection/.test(uc) ? 'collections'
    : /insur|term|life/.test(uc) ? 'insurance'
    : /loan/.test(uc) ? 'personal_loan'
    : /card|credit/.test(uc) ? 'credit_card'
    : 'custom';
  const language = config.language === 'Hindi' ? 'hi' : config.language === 'English' ? 'en' : 'hinglish';
  return {
    name: config.name || 'New Bot',
    use_case,
    language,
    voice_gender: 'female',
    speaking_rate: config.speed,
    greeting_template: config.greeting,
    rag_enabled: config.ragEnabled,
    rag_index: config.ragIndex || null,
    default_tone: config.tone,
    ambient_profile: config.ambientProfile,
  };
}

function backendBotToBot(b: BackendBot): Bot {
  const useCase = USE_CASE_LABEL[b.use_case] || 'Credit Card';
  const langLabel = b.language === 'hi' ? 'Hindi' : b.language === 'en' ? 'English' : 'Hinglish';
  return {
    id: b.id,
    name: b.name,
    useCase,
    language: langLabel,
    status: b.active ? 'Active' : 'Paused',
    calls: 0,
    conversionNum: 0,
    conversion: '—',
    avgDuration: '—',
    escalationRate: '—',
    lastActive: 'live',
    lastActiveTs: Date.now(),
    backendId: b.id,
    backendCustomerId: USE_CASE_CUSTOMER[b.use_case],
  };
}

// Fold real call-log aggregates (AE /v1/stats) into a bot's displayed metrics.
function mergeStats(bot: Bot, stats: BotStats | undefined): Bot {
  if (!stats || stats.total_calls === 0) return bot;
  const conv = stats.conversion_rate * 100;
  const totalSec = Math.round(stats.avg_duration_ms / 1000);
  return {
    ...bot,
    calls: stats.total_calls,
    conversionNum: conv,
    conversion: `${conv.toFixed(1)}%`,
    avgDuration: `${Math.floor(totalSec / 60)}m ${String(totalSec % 60).padStart(2, '0')}s`,
    escalationRate: `${(stats.escalation_rate * 100).toFixed(1)}%`,
  };
}

export default function KoelApp() {
  const [authed, setAuthed] = useState(false);
  // Live backend integration: token + flag. When live, bots/calls hit VB_Svc.
  const [token, setToken] = useState<string | null>(null);
  const [live, setLive] = useState(false);
  // True while we attempt to restore a persisted session on load (Q-2) — avoids
  // flashing the login screen before the silent refresh resolves.
  const [restoring, setRestoring] = useState<boolean>(() => !!loadStoredToken());
  const [currentView, setCurrentView] = useState<ViewState>('dashboard');
  const [activeBotId, setActiveBotId] = useState<string | null>(null);
  const [configBotId, setConfigBotId] = useState<string | null>(null); // Item 1/30
  const [bots, setBots] = useState<Bot[]>(INITIAL_BOTS); // Item 6 — lifted
  const [botConfigs, setBotConfigs] = useState<Record<string, BotConfig>>({}); // Item 5
  const [completedCalls, setCompletedCalls] = useState<CallHistory[]>(MOCK_HISTORY_CALLS);
  const [toast, setToast] = useState<string | null>(null);
  const [darkMode, setDarkMode] = useState(false); // Item 61
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false); // Item 53
  const toastTimerRef = useRef<number | null>(null); // Item 48

  const theme = useMemo(() => getTheme(darkMode), [darkMode]);

  // Item 48: fix overlapping toasts
  const showToast = useCallback((msg: string) => {
    if (toastTimerRef.current) window.clearTimeout(toastTimerRef.current);
    setToast(msg);
    toastTimerRef.current = window.setTimeout(() => setToast(null), 3000);
  }, []);

  // Refresh the relative "last active" label in real time. Call counts come from
  // real backend aggregates (AE), not a random nudge, so they're left untouched.
  useEffect(() => {
    const iv = window.setInterval(() => {
      setBots(prev => prev.map(b => ({
        ...b,
        lastActive: formatRelativeTime(b.lastActiveTs),
      })));
    }, 15000);
    return () => window.clearInterval(iv);
  }, []);

  const navigateToCall = useCallback((botId: string) => {
    setActiveBotId(botId);
    setCurrentView('call');
    setMobileMenuOpen(false);
  }, []);

  // Item 1/30: config bound to bot
  const navigateToConfig = useCallback((botId: string) => {
    setConfigBotId(botId);
    setCurrentView('config');
    setMobileMenuOpen(false);
  }, []);

  const onCallCompleted = useCallback((callData: CallHistory) => {
    setCompletedCalls(prev => [callData, ...prev]);
    // Item 6: update the bot stats after a call
    setBots(prev => prev.map(b =>
      b.name === callData.bot
        ? { ...b, calls: b.calls + 1, lastActive: 'just now', lastActiveTs: Date.now() }
        : b
    ));
  }, []);

  // Item 5: save config
  const handleConfigSave = useCallback(async (botId: string, config: BotConfig) => {
    setBotConfigs(prev => ({ ...prev, [botId]: config }));
    if (botId === 'new') {
      // Live: persist the new bot to VB_Svc so the call pipeline can actually
      // use it (PRD §06 — new use cases are configuration, not code).
      if (live && token) {
        try {
          const created = await createBot(token, configToNewBot(config));
          setBots(prev => [...prev, backendBotToBot(created)]);
          setBotConfigs(prev => ({ ...prev, [created.id]: config }));
          showToast('Bot created on VB_Svc — available for live calls.');
          setCurrentView('dashboard');
          return;
        } catch {
          showToast('Could not save to backend — added locally only.');
        }
      }
      const newId = `b${Date.now().toString(36)}`;
      setBots(prev => [...prev, {
        id: newId, name: config.name, useCase: config.useCase as UseCase, language: config.language,
        status: 'Active', calls: 0, conversionNum: 0, conversion: '0%', avgDuration: '0m 00s',
        escalationRate: '0%', lastActive: 'just now', lastActiveTs: Date.now(),
      }]);
      setBotConfigs(prev => ({ ...prev, [newId]: config }));
    } else if (live && token) {
      // Existing bot → persist the edit to VB_Svc (PATCH) so it survives reload
      // and is audited server-side (S/AF). Falls back to local-only on failure.
      const backendId = bots.find(b => b.id === botId)?.backendId;
      if (backendId) {
        try {
          const { use_case: _uc, ...patch } = configToNewBot(config);
          await patchBot(token, backendId, patch);
          showToast('Configuration saved to VB_Svc — effective on next session start.');
          setCurrentView('dashboard');
          return;
        } catch {
          showToast('Could not save to backend — kept locally only.');
        }
      }
    }
    showToast('Configuration saved — takes effect on next session start.');
    setCurrentView('dashboard');
  }, [showToast, live, token, bots]);

  // Fetch live bots + analytics after auth (fresh login or a restored session).
  // Returns true when real bots loaded; best-effort — the caller owns the toast.
  const hydrateLiveBots = useCallback(async (tok: string): Promise<boolean> => {
    try {
      const backendBots = await apiGetBots(tok);
      if (backendBots.length) {
        // Merge real call-log aggregates (AE) into the loaded bots.
        let stats: BotStats[] = [];
        try {
          stats = await getAllStats(tok);
        } catch {
          /* stats are best-effort; bots still render with zeroed metrics */
        }
        const byId = new Map(stats.map(s => [s.bot_id, s]));
        setBots(backendBots.map(b => mergeStats(backendBotToBot(b), byId.get(b.id))));
        return true;
      }
    } catch {
      /* fall through — caller shows the degraded-mode toast */
    }
    return false;
  }, []);

  const handleLogin = useCallback(async (opts: { live: boolean; token: string | null }) => {
    setLive(opts.live);
    setToken(opts.token);
    setAuthed(true);
    setCurrentView('dashboard');
    if (opts.live && opts.token) {
      saveToken(opts.token); // persist so a reload doesn't log the operator out (Q-2)
      const ok = await hydrateLiveBots(opts.token);
      showToast(ok
        ? 'Connected to live VB_Svc — bots + analytics loaded from the backend.'
        : 'Logged in, but failed to load live bots — showing demo bots.');
    } else {
      showToast('Backend offline — running in demo simulation mode.');
    }
  }, [showToast, hydrateLiveBots]);

  const handleLogout = useCallback(() => {
    clearStoredToken();
    setToken(null);
    setLive(false);
    setAuthed(false);
  }, []);

  // Restore a persisted session on load (Q-2): a still-valid stored token is
  // swapped for a fresh one via /v1/auth/refresh; an expired/invalid one is
  // cleared so the operator simply logs in again. Runs once.
  const restoreAttempted = useRef(false);
  useEffect(() => {
    if (restoreAttempted.current) return;
    restoreAttempted.current = true;
    const stored = loadStoredToken();
    if (!stored) {
      setRestoring(false);
      return;
    }
    (async () => {
      try {
        const fresh = await refreshToken(stored);
        saveToken(fresh);
        setToken(fresh);
        setLive(true);
        setAuthed(true);
        setCurrentView('dashboard');
        await hydrateLiveBots(fresh);
      } catch {
        clearStoredToken(); // expired/invalid → require a fresh login
      } finally {
        setRestoring(false);
      }
    })();
  }, [hydrateLiveBots]);

  // Proactive silent refresh (Q-2): renew the access token at ~80% of its
  // remaining lifetime so a live session never expires mid-shift. Reschedules on
  // every token change; a failed refresh is left for the next 401/reload.
  useEffect(() => {
    if (!live || !token) return;
    const ttl = tokenExpiresInMs(token);
    const delay = ttl && ttl > 0 ? Math.max(ttl * 0.8, 5000) : 600_000;
    const id = window.setTimeout(async () => {
      try {
        const fresh = await refreshToken(token);
        saveToken(fresh);
        setToken(fresh);
      } catch {
        /* refresh failed — a later call's 401 or a reload forces re-login */
      }
    }, delay);
    return () => window.clearTimeout(id);
  }, [live, token]);

  if (restoring) {
    return (
      <div className="min-h-screen flex items-center justify-center" style={{ backgroundColor: theme.primary, fontFamily: '"DM Sans", sans-serif' }}>
        <div className="text-sm text-white/80">Restoring session…</div>
      </div>
    );
  }
  if (!authed) {
    return <LoginView onLogin={handleLogin} theme={theme} />;
  }

  const viewLabels: Record<ViewState, string> = {
    dashboard: 'Bot Dashboard',
    call: 'Active Call',
    config: 'Configuration',
    history: 'Call History',
    builder: 'Bot Builder',
    knowledge: 'Knowledge Base',
  };

  const operateNav: { view: ViewState; icon: React.ReactNode; label: string }[] = [
    { view: 'dashboard', icon: <BarChart2 size={16} />, label: 'Bot Dashboard' },
    { view: 'call', icon: <PhoneCall size={16} />, label: 'Active Call' },
    { view: 'history', icon: <Clock size={16} />, label: 'Call History' },
  ];

  const manageNav: { view: ViewState; icon: React.ReactNode; label: string }[] = [
    { view: 'builder', icon: <Edit3 size={16} />, label: 'Bot Builder' },
    { view: 'config', icon: <Settings size={16} />, label: 'Configuration' },
    { view: 'knowledge', icon: <BookOpen size={16} />, label: 'Knowledge Base' },
  ];

  return (
    <div className={`h-screen overflow-hidden grid grid-cols-1 md:grid-cols-[216px_1fr] ${darkMode ? 'dark' : ''}`} style={{ fontFamily: '"DM Sans", sans-serif', color: theme.ink, backgroundColor: theme.surface }}>
      {/* ---- LEFT SIDEBAR ---- */}
      <aside className="flex flex-col border-r overflow-hidden max-md:hidden" style={{ backgroundColor: theme.sidebarBg, borderColor: theme.rule }}>
        {/* Brand */}
        <div className="flex items-center gap-2.5 px-5 py-4 border-b" style={{ borderColor: theme.ruleSubtle }}>
          <div className="w-7 h-7 flex items-center justify-center text-white text-[15px] font-semibold" style={{ backgroundColor: theme.primary, borderRadius: 7, fontFamily: '"Lora", serif' }}>K</div>
          <div>
            <span className="font-semibold text-lg tracking-tight block leading-tight" style={{ fontFamily: '"Lora", serif', color: theme.ink }}>KOEL</span>
            <span className="text-[10px] tracking-widest uppercase block -mt-0.5" style={{ color: theme.faint }}>Voice Bot Platform</span>
          </div>
        </div>

        {/* Nav */}
        <nav className="flex-1 px-2.5 py-3 flex flex-col gap-0.5 overflow-y-auto">
          <span className="text-[10.5px] font-medium tracking-widest uppercase px-2.5 pt-3 pb-1.5" style={{ color: theme.faint }}>Operate</span>
          {operateNav.map(ni => (
            <NavItem key={ni.view} active={currentView === ni.view} onClick={() => setCurrentView(ni.view)} icon={ni.icon} label={ni.label} theme={theme} />
          ))}
          <span className="text-[10.5px] font-medium tracking-widest uppercase px-2.5 pt-5 pb-1.5" style={{ color: theme.faint }}>Manage</span>
          {manageNav.map(ni => (
            <NavItem key={ni.view} active={currentView === ni.view} onClick={() => setCurrentView(ni.view)} icon={ni.icon} label={ni.label} theme={theme} />
          ))}
        </nav>

        {/* Env status */}
        <div className="mx-2.5 mb-2.5 p-2.5 border rounded-lg" style={{ backgroundColor: theme.inset, borderColor: theme.rule }}>
          <div className="flex justify-between items-center text-xs" style={{ color: theme.inkMuted }}>
            <span className="flex items-center gap-1.5">
              <span className="w-[7px] h-[7px] rounded-full inline-block" style={{ backgroundColor: theme.greenMid }}></span>
              VB_Svc
            </span>
            <span style={{ fontFamily: '"DM Mono", monospace', fontSize: 11, color: theme.ink2 }}>ws · connected</span>
          </div>
          <div className="flex justify-between items-center text-xs mt-1.5" style={{ color: theme.faint }}>
            <span style={{ fontSize: 11.5 }}>Env</span>
            <span style={{ fontFamily: '"DM Mono", monospace', fontSize: 11, color: theme.ink2 }}>demo-mvp</span>
          </div>
        </div>
      </aside>

      {/* ---- MAIN COLUMN ---- */}
      <div className="flex flex-col overflow-hidden">
        {/* Topbar */}
        <header className="h-14 flex items-center justify-between px-6 border-b flex-none" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
          <div className="flex items-center gap-2">
            {/* Mobile hamburger */}
            <button onClick={() => setMobileMenuOpen(!mobileMenuOpen)} className="md:hidden p-2 rounded-lg mr-2" style={{ color: theme.inkMuted }} aria-label="Toggle navigation menu">
              {mobileMenuOpen ? <X size={20} /> : <Menu size={20} />}
            </button>
            {/* Mobile logo */}
            <div className="md:hidden flex items-center gap-2 mr-3 cursor-pointer" onClick={() => setCurrentView('dashboard')}>
              <div className="w-6 h-6 flex items-center justify-center text-white text-xs font-semibold" style={{ backgroundColor: theme.primary, borderRadius: 5, fontFamily: '"Lora", serif' }}>K</div>
              <span className="font-semibold text-base" style={{ fontFamily: '"Lora", serif', color: theme.primary }}>KOEL</span>
            </div>
            {/* Breadcrumb */}
            <span className="hidden md:inline text-[13px]" style={{ color: theme.inkMuted }}>KOEL&nbsp;&nbsp;/&nbsp;&nbsp;<b style={{ color: theme.ink, fontWeight: 500 }}>{viewLabels[currentView]}</b></span>
          </div>
          <div className="flex items-center gap-3">
            <button onClick={() => setDarkMode(!darkMode)} className="w-8 h-8 rounded-full flex items-center justify-center transition-colors" style={{ color: theme.inkMuted }} aria-label={darkMode ? 'Switch to light mode' : 'Switch to dark mode'}>
              {darkMode ? <Sun size={15} /> : <Moon size={15} />}
            </button>
            <div className="hidden sm:flex items-center gap-1.5 px-2 py-1 rounded-full text-[11.5px] font-medium" style={{ backgroundColor: theme.greenLight, color: theme.green }}>
              <span className="w-[6px] h-[6px] rounded-full" style={{ backgroundColor: 'currentColor' }}></span>
              All services healthy
            </div>
            <div className="w-[30px] h-[30px] rounded-full flex items-center justify-center text-xs font-medium" style={{ backgroundColor: theme.primaryLight, color: theme.primary }}>GA</div>
          </div>
        </header>

        {/* Mobile nav overlay */}
        {mobileMenuOpen && (
          <div className="md:hidden border-b p-4 flex flex-col gap-1 animate-in fade-in" style={{ backgroundColor: theme.surfaceCard, borderColor: theme.rule }}>
            {[...operateNav, ...manageNav].map(ni => (
              <button key={ni.view} onClick={() => { setCurrentView(ni.view); setMobileMenuOpen(false); }} className="flex items-center gap-2.5 px-3 py-2.5 rounded-md text-sm" style={{ backgroundColor: currentView === ni.view ? theme.primaryLight : 'transparent', color: currentView === ni.view ? theme.primary : theme.ink }}>
                {ni.icon} {ni.label}
              </button>
            ))}
            <hr className="my-2" style={{ borderColor: theme.rule }} />
            <button onClick={() => setDarkMode(!darkMode)} className="flex items-center gap-2.5 px-3 py-2.5 rounded-md text-sm" style={{ color: theme.ink }}>
              {darkMode ? <Sun size={16} /> : <Moon size={16} />} {darkMode ? 'Light Mode' : 'Dark Mode'}
            </button>
            <button onClick={handleLogout} className="flex items-center gap-2.5 px-3 py-2.5 rounded-md text-sm text-red-600">
              <LogOut size={16} /> Log Out
            </button>
          </div>
        )}

        {/* Screen content */}
        <main className="flex-1 overflow-y-auto">
          {currentView === 'dashboard' && <DashboardView bots={bots} onLaunch={navigateToCall} onConfig={navigateToConfig} onBuilder={() => setCurrentView('builder')} theme={theme} />}
          {currentView === 'call' && (
            activeBotId ? (
              <ActiveCallView
                bot={bots.find(b => b.id === activeBotId) || bots[0]}
                onBack={() => setCurrentView('dashboard')}
                onCallCompleted={onCallCompleted}
                theme={theme}
                darkMode={darkMode}
                live={live}
                token={token}
              />
            ) : (
              <div className="flex flex-col items-center justify-center h-[60vh] text-center p-8">
                <PhoneCall size={64} className="mb-6 opacity-20" style={{ color: theme.primary }} />
                <p className="text-xl font-semibold mb-2" style={{ fontFamily: '"Lora", serif', color: theme.ink }}>No Bot Selected</p>
                <p className="text-[13px] max-w-sm" style={{ color: theme.inkMuted }}>Select a bot from the Dashboard and click "Launch Sandbox" to open the active call view.</p>
                <button onClick={() => setCurrentView('dashboard')} className="mt-6 px-5 py-2.5 rounded-md text-white font-medium text-sm" style={{ backgroundColor: theme.primary }}>Go to Dashboard</button>
              </div>
            )
          )}
          {currentView === 'config' && (
            <ConfigView
              bot={configBotId && configBotId !== 'new' ? bots.find(b => b.id === configBotId) : undefined}
              existingConfig={configBotId ? botConfigs[configBotId] : undefined}
              isNew={configBotId === 'new'}
              onSave={(config) => handleConfigSave(configBotId || 'new', config)}
              onCancel={() => setCurrentView('dashboard')}
              theme={theme}
            />
          )}
          {currentView === 'history' && <HistoryView calls={completedCalls} onExported={() => showToast('CSV exported for QA workflows.')} theme={theme} darkMode={darkMode} />}
          {currentView === 'builder' && <BotBuilderView onDeploy={(config) => { handleConfigSave('new', config); showToast('Bot deployed — available on the dashboard.'); }} onCancel={() => setCurrentView('dashboard')} theme={theme} />}
          {currentView === 'knowledge' && <KnowledgeBaseView theme={theme} token={token} onToast={showToast} />}
        </main>
      </div>

      {toast && (
        <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-[100] bg-gray-900 text-white text-sm font-medium px-5 py-3 rounded-lg shadow-xl flex items-center gap-2 animate-in fade-in slide-in-from-bottom-2" role="status" aria-live="polite">
          <CheckCircle2 size={16} className="text-green-400" /> {toast}
        </div>
      )}
    </div>
  );
}

// ============================================================================
// Login — PRD Section 8: dashboard behind login, seeded demo credentials, JWT
// Items 21 (Turnstile), 60 (credential validation)
// ============================================================================
// ============================================================================
// Shared nav item
// ============================================================================
interface NavItemProps { active: boolean; onClick: () => void; icon: React.ReactNode; label: string; theme: ReturnType<typeof getTheme> }

function NavItem({ active, onClick, icon, label, theme }: NavItemProps) {
  return (
    <button onClick={onClick} className={`flex items-center gap-2.5 px-2.5 py-2 rounded-md text-[13.5px] w-full text-left transition-colors ${active ? 'font-medium' : ''}`} style={{ backgroundColor: active ? theme.primaryLight : 'transparent', color: active ? theme.primary : theme.ink2 }}>
      {icon}
      {label}
    </button>
  );
}

// ============================================================================
// Agent Handoff Panel (Item 42)
// ============================================================================
function AgentHandoffPanel({ theme, darkMode }: { theme: ReturnType<typeof getTheme>, darkMode: boolean }) {
  const [step, setStep] = useState(0);

  useEffect(() => {
    const t1 = setTimeout(() => setStep(1), 600);
    const t2 = setTimeout(() => setStep(2), 1200);
    const t3 = setTimeout(() => setStep(3), 1800);
    return () => { clearTimeout(t1); clearTimeout(t2); clearTimeout(t3); };
  }, []);

  return (
    <div className="absolute inset-0 z-50 flex items-center justify-center p-6 backdrop-blur-md animate-in fade-in duration-300" style={{ backgroundColor: darkMode ? 'rgba(15, 23, 42, 0.75)' : 'rgba(255, 255, 255, 0.75)' }}>
      <div className="w-full max-w-sm rounded-lg border shadow-2xl overflow-hidden animate-in zoom-in-95 duration-500" style={{ backgroundColor: theme.surfaceCard, borderColor: theme.rule }}>
        <div className="p-4 border-b flex items-center gap-3" style={{ borderColor: theme.rule, backgroundColor: darkMode ? '#1E293B' : '#F8FAFC' }}>
          <div className="w-10 h-10 rounded-full flex items-center justify-center bg-blue-100 text-blue-600">
            <User size={20} />
          </div>
          <div>
            <h3 className="font-bold text-sm" style={{ color: theme.ink }}>Agent Console</h3>
            <p className="text-[11px]" style={{ color: theme.inkMuted }}>Handoff Initiated</p>
          </div>
        </div>
        <div className="p-5 flex flex-col gap-4">
          <div className="flex items-center justify-between text-sm">
            <span style={{ color: theme.ink }} className="font-medium">Full Transcript</span>
            {step >= 1 ? <CheckCircle2 size={18} className="text-green-500 animate-in zoom-in" /> : <div className="w-4 h-4 rounded-full border-2 border-gray-200 border-t-blue-500 animate-spin" />}
          </div>
          <div className="flex items-center justify-between text-sm">
            <span style={{ color: theme.ink }} className="font-medium">CRM Context</span>
            {step >= 2 ? <CheckCircle2 size={18} className="text-green-500 animate-in zoom-in" /> : <div className="w-4 h-4 rounded-full border-2 border-gray-200 border-t-blue-500 animate-spin" />}
          </div>
          <div className="flex items-center justify-between text-sm">
            <span style={{ color: theme.ink }} className="font-medium">Intent History</span>
            {step >= 3 ? <CheckCircle2 size={18} className="text-green-500 animate-in zoom-in" /> : <div className="w-4 h-4 rounded-full border-2 border-gray-200 border-t-blue-500 animate-spin" />}
          </div>
        </div>
      </div>
    </div>
  );
}

// ============================================================================
// Screen 1 — Bot Selection Dashboard (PRD Section 9, Screen 1)
// Items 6, 8, 29, 36, 40
// ============================================================================
interface DashboardViewProps { bots: Bot[]; onLaunch: (id: string) => void; onConfig: (id: string) => void; onBuilder: () => void; theme: ReturnType<typeof getTheme> }

function DashboardView({ bots, onLaunch, onBuilder, theme }: DashboardViewProps) {
  const [sortBy] = useState<string>('calls');
  const [search, setSearch] = useState('');

  const filteredBots = bots.filter(
    b => !search || `${b.name} ${b.useCase}`.toLowerCase().includes(search.toLowerCase())
  );
  const sortedBots = [...filteredBots].sort((a, b) => {
    if (sortBy === 'calls') return b.calls - a.calls;
    if (sortBy === 'conversion') return b.conversionNum - a.conversionNum;
    if (sortBy === 'name') return a.name.localeCompare(b.name);
    return 0;
  });

  const glyphColors: Record<string, { bg: string; fg: string }> = {
    b1: { bg: theme.primaryLight, fg: theme.primary },
    b2: { bg: theme.greenLight, fg: theme.green },
    b3: { bg: theme.accentLight, fg: '#B23A14' },
    b4: { bg: theme.primaryLight, fg: theme.primary },
  };

  // Live KPI aggregates (Q-3): derived from the same per-bot stats the cards use
  // — real `/v1/stats` aggregates when connected to the backend, the shown demo
  // bots otherwise — so the strip always reflects real data, never hardcoded
  // numbers. Rates are weighted by each bot's call volume for a true overall.
  const kpiTotalCalls = bots.reduce((sum, b) => sum + (b.calls || 0), 0);
  const kpiConvPer100 = kpiTotalCalls
    ? bots.reduce((sum, b) => sum + (b.calls || 0) * (b.conversionNum || 0), 0) / kpiTotalCalls
    : 0;
  const kpiEscPct = kpiTotalCalls
    ? bots.reduce((sum, b) => {
        const rate = parseFloat(b.escalationRate); // "9.1%" → 9.1; "—" → NaN (skipped)
        return sum + (Number.isFinite(rate) ? (b.calls || 0) * rate : 0);
      }, 0) / kpiTotalCalls
    : 0;

  return (
    <div className="p-5 pb-8 animate-in fade-in duration-300" style={{ maxWidth: 1200, margin: '0 auto' }}>
      <div className="flex flex-wrap justify-between items-end mb-4 gap-4">
        <div>
          <h1 className="text-[19px] font-semibold tracking-tight" style={{ color: theme.ink }}>Bot Dashboard</h1>
          <p className="text-[13px] mt-0.5" style={{ color: theme.inkMuted }}>{bots.filter(b => b.status === 'Active').length} active bot instances · last sync 2 min ago</p>
        </div>
        <div className="flex items-center gap-3">
          <div className="relative">
            <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2" style={{ color: theme.faint }} />
            <input
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder="Search bots…"
              className="border rounded-md pl-9 pr-3 py-2 text-[13px] outline-none focus:ring-2 w-40 md:w-56"
              style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard, color: theme.ink, fontFamily: '"DM Sans", sans-serif' }}
            />
          </div>
          <button onClick={onBuilder} className="flex items-center gap-2 px-3.5 py-[7px] rounded-md text-[13.5px] text-white font-normal transition-colors hover:opacity-90" style={{ backgroundColor: theme.primary }}>
            <Plus size={15} /> New bot
          </button>
        </div>
      </div>

      {/* KPI strip — 4 cards matching design */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3.5 mb-5">
        <KpiCard label="Connected calls" value={kpiTotalCalls.toLocaleString()} meta={`${bots.length} bots`} metaDir="flat" theme={theme} />
        <KpiCard label="First-audio · P95" value="2.3" unit="s" meta="measured baseline · perceived <300ms via filler (SLO.md)" metaDir="flat" theme={theme} />
        <KpiCard label="Warm leads / 100 calls" value={kpiConvPer100.toFixed(1)} meta="conversion across bots" metaDir="flat" theme={theme} />
        <KpiCard label="Escalation rate" value={`${kpiEscPct.toFixed(1)}%`} meta="100% with full context handoff" metaDir="flat" theme={theme} />
      </div>

      {search && sortedBots.length === 0 && (
        <div className="text-[13px] text-center py-8" style={{ color: theme.inkMuted }}>
          No bots match “{search}”.
        </div>
      )}

      {/* Bot grid — 3 columns */}
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3.5">
        {sortedBots.map(bot => {
          const gc = glyphColors[bot.id] || { bg: theme.primaryLight, fg: theme.primary };
          return (
            <div key={bot.id} onClick={() => onLaunch(bot.id)} className="border rounded-lg p-4 flex flex-col gap-3 cursor-pointer transition-colors" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }} onMouseEnter={e => (e.currentTarget.style.borderColor = theme.primary)} onMouseLeave={e => (e.currentTarget.style.borderColor = theme.rule)}>
              <div className="flex items-start justify-between gap-2.5">
                <div className="flex items-center gap-2.5 min-w-0">
                  <div className="w-9 h-9 rounded-md flex items-center justify-center flex-none text-[15px] font-medium" style={{ backgroundColor: gc.bg, color: gc.fg }}>{bot.name[0]}</div>
                  <div className="min-w-0">
                    <div className="text-sm font-medium truncate" style={{ color: theme.ink }}>{bot.name}</div>
                    <div className="text-xs mt-0.5" style={{ color: theme.inkMuted }}>{bot.useCase} · Outgoing</div>
                  </div>
                </div>
                {bot.status === 'Active' ? (
                  <span className="flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[11.5px] font-medium whitespace-nowrap" style={{ backgroundColor: theme.greenLight, color: theme.green }}>
                    <span className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: 'currentColor' }}></span>Live
                  </span>
                ) : (
                  <span className="flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[11.5px] font-medium whitespace-nowrap border" style={{ backgroundColor: theme.inset, color: theme.inkMuted, borderColor: theme.ruleSubtle }}>Paused</span>
                )}
              </div>

              {/* Stat row — calls, conversion, avg duration, escalation (PRD §09 Screen 1) */}
              <div className="grid grid-cols-4 rounded-md overflow-hidden border" style={{ borderColor: theme.ruleSubtle, gap: 1, backgroundColor: theme.ruleSubtle }}>
                <div className="py-1.5 px-2.5" style={{ backgroundColor: theme.inset }}>
                  <div className="text-[10.5px] uppercase tracking-wide" style={{ color: theme.faint }}>Calls</div>
                  <div className="text-sm font-medium mt-0.5" style={{ color: theme.ink, fontVariantNumeric: 'tabular-nums' }}>{bot.calls.toLocaleString()}</div>
                </div>
                <div className="py-1.5 px-2.5" style={{ backgroundColor: theme.inset }}>
                  <div className="text-[10.5px] uppercase tracking-wide" style={{ color: theme.faint }}>Conv.</div>
                  <div className="text-sm font-medium mt-0.5" style={{ color: theme.ink, fontVariantNumeric: 'tabular-nums' }}>{bot.conversion}</div>
                </div>
                <div className="py-1.5 px-2.5" style={{ backgroundColor: theme.inset }}>
                  <div className="text-[10.5px] uppercase tracking-wide" style={{ color: theme.faint }}>Avg dur</div>
                  <div className="text-sm font-medium mt-0.5" style={{ color: theme.ink, fontVariantNumeric: 'tabular-nums' }}>{bot.avgDuration}</div>
                </div>
                <div className="py-1.5 px-2.5" style={{ backgroundColor: theme.inset }}>
                  <div className="text-[10.5px] uppercase tracking-wide" style={{ color: theme.faint }}>Esc.</div>
                  <div className="text-sm font-medium mt-0.5" style={{ color: theme.ink, fontVariantNumeric: 'tabular-nums' }}>{bot.escalationRate}</div>
                </div>
              </div>

              {/* Footer */}
              <div className="flex justify-between items-center text-xs" style={{ color: theme.faint }}>
                <div className="flex gap-1">
                  {bot.language.split(/[,/]/).map(l => l.trim()).map(l => (
                    <span key={l} className="px-[7px] py-px rounded-full border text-[10.5px]" style={{ borderColor: theme.rule, color: theme.inkMuted }}>{l}</span>
                  ))}
                </div>
                <span>Active {bot.lastActive}</span>
              </div>
            </div>
          );
        })}

        {/* Add new bot card */}
        <button onClick={onBuilder} className="rounded-lg flex flex-col items-center justify-center gap-2 min-h-[170px] cursor-pointer transition-colors" style={{ border: '1.5px dashed ' + theme.rule, color: theme.inkMuted, background: 'transparent' }} onMouseEnter={e => { e.currentTarget.style.borderColor = theme.primary; e.currentTarget.style.color = theme.primary; e.currentTarget.style.backgroundColor = theme.primarySubtle; }} onMouseLeave={e => { e.currentTarget.style.borderColor = theme.rule; e.currentTarget.style.color = theme.inkMuted; e.currentTarget.style.backgroundColor = 'transparent'; }}>
          <div className="w-[34px] h-[34px] rounded-full border-[1.5px] flex items-center justify-center" style={{ borderColor: 'currentColor' }}>
            <Plus size={15} />
          </div>
          <span className="text-[13px] font-medium">Add new bot</span>
          <span className="text-[11.5px]" style={{ color: theme.faint }}>Form-based setup · no dev work required</span>
        </button>
      </div>
    </div>
  );
}

function KpiCard({ label, value, unit, meta, metaDir, theme }: { label: string; value: string; unit?: string; meta: string; metaDir: 'up' | 'down' | 'flat'; theme: ReturnType<typeof getTheme> }) {
  return (
    <div className="border rounded-lg px-4 py-3.5" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
      <p className="text-xs" style={{ color: theme.inkMuted }}>{label}</p>
      <p className="text-2xl font-semibold tracking-tight mt-1" style={{ color: theme.ink, fontVariantNumeric: 'tabular-nums' }}>
        {value}{unit && <span className="text-sm font-normal ml-0.5" style={{ color: theme.inkMuted }}>{unit}</span>}
      </p>
      <p className="text-[11.5px] mt-1 flex items-center gap-1.5" style={{ color: metaDir === 'up' ? theme.green : metaDir === 'down' ? theme.destructive : theme.faint }}>
        {metaDir === 'up' && <>&#9650; {meta}</>}
        {metaDir === 'down' && <>&#9660; {meta}</>}
        {metaDir === 'flat' && meta}
      </p>
    </div>
  );
}

function IntentPanel({ callState, theme }: { callState: CallState; theme: ReturnType<typeof getTheme> }) {
  const idle = callState === 'idle' || callState === 'ended';
  const intents = [
    { label: 'High interest', score: 0.86, lead: true },
    { label: 'Needs detail', score: 0.62, lead: false },
    { label: 'Objection — has cards', score: 0.18, lead: false },
    { label: 'Hard rejection', score: 0.04, lead: false },
  ];
  return (
    <div className="flex flex-col gap-1.5">
      {intents.map(i => (
        <div key={i.label} className="flex items-center gap-2 py-1.5 px-2 rounded-md text-[12.5px]">
          <span className="flex-none w-28 truncate" style={{ color: theme.ink }}>{i.label}</span>
          <div className="flex-1 h-[5px] rounded-full overflow-hidden" style={{ backgroundColor: theme.ruleSubtle }}>
            <div className="h-full rounded-full transition-all duration-700" style={{ width: idle ? '0%' : `${i.score * 100}%`, backgroundColor: i.lead ? theme.primary : theme.faint }}></div>
          </div>
          <span className="w-8 text-right text-[11.5px] font-medium" style={{ fontFamily: '"DM Mono", monospace', color: i.lead ? theme.primary : theme.inkMuted }}>
            {idle ? '—' : `.${String(Math.round(i.score * 100)).padStart(2, '0')}`}
          </span>
        </div>
      ))}
    </div>
  );
}

// ============================================================================
// Screen 3 — Active Call Sandbox (PRD Section 9, Screen 3 + Section 5)
// ============================================================================
interface ActiveCallViewProps {
  bot: Bot;
  onBack: () => void;
  onCallCompleted: (call: CallHistory) => void;
  theme: ReturnType<typeof getTheme>;
  darkMode: boolean;
  live?: boolean;
  token?: string | null;
}

function ActiveCallView({ bot, onBack, onCallCompleted, theme, darkMode, live = false, token = null }: ActiveCallViewProps) {
  const scenario = SCENARIOS[bot.useCase];
  const customer = MOCK_CUSTOMERS[bot.useCase];
  // True only when this bot is backed by VB_Svc AND we hold a token.
  const liveCall = live && !!token && !!bot.backendId;

  const [callState, setCallState] = useState<CallState>('idle');
  const [transcript, setTranscript] = useState<TranscriptMessageData[]>([]);
  const [events, setEvents] = useState<SessionEvent[]>([]);
  const [duration, setDuration] = useState<number>(0);
  const [expandedInteraction, setExpandedInteraction] = useState<string | null>(null);
  const [noInputCountdown, setNoInputCountdown] = useState<number | null>(null);
  const [graceCountdown, setGraceCountdown] = useState<number | null>(null);
  const [sessionId, setSessionId] = useState<string>('');
  const [soundEnabled, setSoundEnabled] = useState(false); // Item 52

  const timerRef = useRef<number | null>(null);
  const pendingTimeouts = useRef<number[]>([]);
  const noInputTimerRef = useRef<number | null>(null);
  const graceIntervalRef = useRef<number | null>(null); // Items 15/22: track grace interval
  const rePromptedRef = useRef(false);
  const outcomeRef = useRef<{ label: string; status: CallHistory['status'] }>({ label: 'Completed', status: 'green' });
  const transcriptRef = useRef<TranscriptMessageData[]>([]);
  const durationRef = useRef(0);
  const endedRef = useRef(false);
  const lastFillerRef = useRef<number>(-1);
  const transcriptEndRef = useRef<HTMLDivElement>(null);
  const eventsEndRef = useRef<HTMLDivElement>(null); // Item 50
  const eventsContainerRef = useRef<HTMLDivElement>(null);
  const firstUserMsgRef = useRef(false); // Track first user message for Items 14, 18
  const callStartTimeRef = useRef(0); // Item 24: wall-clock start time
  const noReconnectRef = useRef(false); // Item 11: user chose no reconnect
  const liveCallRef = useRef<LiveCall | null>(null); // live WS session
  const liveCrmRef = useRef<Record<string, unknown>>({}); // CRM context from session init
  const liveReconnectRef = useRef<(() => void) | null>(null); // re-bind socket within grace window

  // Item 49: sync refs in useEffect instead of render
  useEffect(() => { transcriptRef.current = transcript; }, [transcript]);
  useEffect(() => { durationRef.current = duration; }, [duration]);

  const audioCtxRef = useRef<AudioContext | null>(null);
  // Real AnalyserNode in the playback graph so the waveform reflects the actual
  // bot audio (voice + server-mixed call-floor bed), not a synthetic oscillator.
  const analyserRef = useRef<AnalyserNode | null>(null);
  const masterGainRef = useRef<GainNode | null>(null);
  const audioQueueRef = useRef<AudioBuffer[]>([]);
  const audioPlayingRef = useRef(false);
  const audioSrcRef = useRef<AudioBufferSourceNode | null>(null);

  // Lazily build the playback graph: source -> analyser -> masterGain -> output.
  // masterGain is the mute switch; the analyser always sees audio so the
  // visualizer stays amplitude-reactive even when muted.
  const ensureAudioGraph = (): AudioContext | null => {
    if (audioCtxRef.current) return audioCtxRef.current;
    const Ctor = window.AudioContext || (window as any).webkitAudioContext;
    if (!Ctor) return null;
    const ctx: AudioContext = new Ctor();
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 64;
    const gain = ctx.createGain();
    gain.gain.value = soundEnabled ? 1 : 0;
    analyser.connect(gain);
    gain.connect(ctx.destination);
    audioCtxRef.current = ctx;
    analyserRef.current = analyser;
    masterGainRef.current = gain;
    return ctx;
  };

  const playNextAudio = () => {
    const ctx = audioCtxRef.current;
    const analyser = analyserRef.current;
    const buf = audioQueueRef.current.shift();
    if (!buf || !ctx || !analyser) {
      // Queue drained after playing → tell the server the bot truly finished so
      // the no-input timer starts now, not from the send-time estimate.
      if (audioPlayingRef.current) liveCallRef.current?.playbackDone();
      audioPlayingRef.current = false;
      return;
    }
    audioPlayingRef.current = true;
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(analyser);
    src.onended = () => { if (audioSrcRef.current === src) audioSrcRef.current = null; playNextAudio(); };
    audioSrcRef.current = src;
    src.start();
  };

  // Decode a base64 WAV chunk from the backend and enqueue it for gapless playback.
  const enqueueAudio = async (audio_b64?: string) => {
    if (!audio_b64) return;
    const ctx = ensureAudioGraph();
    if (!ctx) return;
    if (ctx.state === 'suspended') { try { await ctx.resume(); } catch { /* user-gesture gated */ } }
    try {
      const bytes = Uint8Array.from(atob(audio_b64), (c) => c.charCodeAt(0));
      const buffer = await ctx.decodeAudioData(bytes.buffer);
      audioQueueRef.current.push(buffer);
      if (!audioPlayingRef.current) playNextAudio();
    } catch { /* undecodable chunk — skip rather than break the call */ }
  };

  // Stop playback immediately (barge-in TTS_KILL, or call end) and drop the queue.
  const flushPlayback = () => {
    audioQueueRef.current = [];
    if (audioSrcRef.current) {
      try { audioSrcRef.current.onended = null; audioSrcRef.current.stop(); } catch { /* already stopped */ }
      audioSrcRef.current = null;
    }
    audioPlayingRef.current = false;
  };

  // Keep the mute switch in sync; release playback when the call is not active.
  useEffect(() => {
    if (masterGainRef.current) masterGainRef.current.gain.value = soundEnabled ? 1 : 0;
    if (callState === 'idle' || callState === 'ended') flushPlayback();
  }, [soundEnabled, callState]);

  // --- real microphone capture (live mode) ---
  const [micOn, setMicOn] = useState(false);
  const micRef = useRef<MicCapture | null>(null);

  const stopMic = () => {
    micRef.current?.stop();
    micRef.current = null;
    setMicOn(false);
  };

  const toggleMic = async () => {
    if (micOn) { stopMic(); return; }
    if (!liveCall) { logEvent('Microphone is only available in live mode (real VB_Svc).', 'warn'); return; }
    try {
      const cap = new MicCapture({
        onUtterance: (b64, fmt, pcm16) => {
          logEvent(`Mic utterance captured (${fmt}) → streaming to STT`, 'info');
          liveCallRef.current?.sendAudioChunk(b64, fmt, pcm16);
        },
        // Real barge-in: the instant the caller speaks over the bot, cut the bot's
        // audio locally (feels like a real interruption) and tell VB_Svc so it stops
        // streaming the rest of the turn. Echo cancellation keeps the bot's own
        // audio from self-triggering this.
        onSpeechStart: () => {
          if (audioPlayingRef.current) {
            flushPlayback();
            liveCallRef.current?.bargeIn('', 0);
            logEvent('Barge-in — caller spoke over the bot; playback halted', 'info');
          }
        },
        onError: (err) => logEvent(`Mic error: ${err instanceof Error ? err.message : String(err)}`, 'warn'),
      });
      await cap.start();
      micRef.current = cap;
      setMicOn(true);
      logEvent('Microphone armed — speak; VAD endpoints each utterance', 'ok');
    } catch (err) {
      logEvent(`Microphone access denied/unavailable: ${err instanceof Error ? err.message : String(err)}`, 'warn');
      setMicOn(false);
    }
  };

  // Release the mic when the call ends and on unmount.
  useEffect(() => {
    if (callState === 'idle' || callState === 'ended') stopMic();
  }, [callState]);
  useEffect(() => () => stopMic(), []);

  const demoActiveRef = useRef(false);
  const [demoStepTooltip, setDemoStepTooltip] = useState<{ id: string, text: string } | null>(null);

  // Guided auto-demo driven against the real backend (live mode).
  const runGuidedDemoLive = () => {
    demoActiveRef.current = true;
    startCall();
    let time = 2500; // greeting arrives fast over the socket
    schedule(() => { if (demoActiveRef.current) { setDemoStepTooltip({ id: 'chip-1', text: 'Customer raises an objection' }); handleChip(scenario.chips[1] || scenario.chips[0]); } }, time);
    time += 3500;
    schedule(() => { if (demoActiveRef.current) { setDemoStepTooltip({ id: 'barge-in', text: 'Customer interrupts (barge-in)' }); handleBargeIn(); } }, time);
    time += 4000;
    schedule(() => { if (demoActiveRef.current) { setDemoStepTooltip({ id: 'escalate', text: 'Customer asks for a human agent' }); liveCallRef.current?.sendText('Mujhe kisi agent se baat karni hai please'); } }, time);
    schedule(() => { demoActiveRef.current = false; setDemoStepTooltip(null); }, time + 4000);
  };

  // Item 51: Auto-demo orchestrator
  const runGuidedDemo = () => {
    if (liveCall) { runGuidedDemoLive(); return; }
    demoActiveRef.current = true;
    startCall();

    let time = 3500; // Let greeting finish
    
    // Step 1: Normal Response
    schedule(() => {
      if (!demoActiveRef.current) return;
      setDemoStepTooltip({ id: 'chip-0', text: 'Simulating positive customer response' });
      simulateUserResponse(scenario.chips[0]);
    }, time);
    time += 6000;
    
    // Step 2: Barge In
    schedule(() => {
      if (!demoActiveRef.current) return;
      setDemoStepTooltip({ id: 'barge-in', text: 'Simulating interruption (Barge-In)' });
      simulateBargeIn();
    }, time);
    time += 6000;
    
    // Step 3: Noise
    schedule(() => {
      if (!demoActiveRef.current) return;
      setDemoStepTooltip({ id: 'noise', text: 'Simulating background noise (filtered by VAD)' });
      simulateBackgroundNoise();
    }, time);
    time += 4000;
    
    // Step 4: Language Switch
    schedule(() => {
      if (!demoActiveRef.current) return;
      setDemoStepTooltip({ id: 'lang-switch', text: 'Customer switches language mid-call' });
      simulateLanguageSwitch();
    }, time);
    time += 6000;
    
    // Step 5: Escalation
    schedule(() => {
      if (!demoActiveRef.current) return;
      setDemoStepTooltip({ id: 'escalate', text: 'Customer requests agent' });
      simulateUserResponse({ 
        label: 'Speak to Agent', 
        userText: 'Mujhe ek human agent se baat karni hai, ye bot se nahi hoga.', 
        botReply: 'Okay, I understand. Please hold while I connect you to an agent who can help you further.', 
        tone: 'empathetic', 
        action: 'ESCALATE' 
      });
    }, time);
    
    schedule(() => {
      demoActiveRef.current = false;
      setDemoStepTooltip(null);
    }, time + 4000);
  };

  // --- low-level helpers ---
  const getSpeechDuration = (text: string) => Math.max(1500, text.length * 60);

  const schedule = (fn: () => void, ms: number) => {
    const t = window.setTimeout(fn, ms);
    pendingTimeouts.current.push(t);
    return t;
  };
  // Items 15/22: clear grace interval too
  const clearAllScheduled = () => {
    pendingTimeouts.current.forEach(t => window.clearTimeout(t));
    pendingTimeouts.current = [];
    if (noInputTimerRef.current) { window.clearInterval(noInputTimerRef.current); noInputTimerRef.current = null; }
    if (graceIntervalRef.current) { window.clearInterval(graceIntervalRef.current); graceIntervalRef.current = null; }
    setNoInputCountdown(null);
    setGraceCountdown(null);
  };

  // Item 49: generate id before setState
  const addMsg = (msg: Omit<TranscriptMessageData, 'id'>) => {
    const id = nextId();
    const timestamp = durationRef.current;
    setTranscript(prev => [...prev, { ...msg, id, timestamp }]);
    // Demo mode only: synthesize bot speech in the browser. In live mode the real
    // bot audio (Orpheus/Magpie + ambient bed) streams from VB_Svc and is played
    // via enqueueAudio(), so browser SpeechSynthesis would double up — skip it.
    if (!liveCall && soundEnabled && msg.kind === 'bot' && typeof window !== 'undefined' && window.speechSynthesis) {
      const utter = new SpeechSynthesisUtterance(msg.text);
      utter.rate = msg.tone === 'firm' ? 0.9 : msg.tone === 'cheerful' ? 1.1 : 1.0;
      utter.pitch = msg.tone === 'empathetic' ? 0.9 : 1.0;
      window.speechSynthesis.speak(utter);
    }
  };

  const logEvent = (label: string, level: SessionEvent['level'] = 'info') => {
    const id = nextId();
    setEvents(prev => [...prev, { id, t: formatTime(durationRef.current), label, level }]);
  };

  // --- transcript autoscroll / call timer ---
  useEffect(() => {
    transcriptEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [transcript, callState]);

  // Item 50: OTel event auto-scroll
  useEffect(() => {
    eventsEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [events]);

  // Item 24: fix duration timer — depend on derived isLive, not callState
  const isLive = callState !== 'idle' && callState !== 'ended';
  useEffect(() => {
    if (isLive) {
      timerRef.current = window.setInterval(() => setDuration(d => d + 1), 1000);
    } else if (timerRef.current !== null) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
    return () => { if (timerRef.current !== null) { window.clearInterval(timerRef.current); timerRef.current = null; } };
  }, [isLive]);

  // Cleanup on unmount — also finalize + close any live socket so navigating
  // away mid-call (e.g. "Exit Sandbox") ends the backend call cleanly instead
  // of leaking the WebSocket and waiting for the 15s grace TTL to expire.
  useEffect(() => () => {
    clearAllScheduled();
    if (timerRef.current) window.clearInterval(timerRef.current);
    if (liveCallRef.current) {
      try { liveCallRef.current.endCall(); } catch { /* socket may be gone */ }
      liveCallRef.current.close();
      liveCallRef.current = null;
    }
  }, []);

  // --- no-input watchdog (PRD: 5s → re-prompt, further 5s → polite close) ---
  // In live mode VB_Svc owns this timer (it re-prompts/closes over the socket),
  // so the simulated watchdog is disabled to avoid double-firing.
  useEffect(() => {
    if (liveCall) return;
    if (callState !== 'listening') {
      if (noInputTimerRef.current) { window.clearInterval(noInputTimerRef.current); noInputTimerRef.current = null; }
      setNoInputCountdown(null);
      return;
    }
    let remaining = 5;
    setNoInputCountdown(remaining);
    noInputTimerRef.current = window.setInterval(() => {
      remaining -= 1;
      setNoInputCountdown(remaining);
      if (remaining <= 0) {
        window.clearInterval(noInputTimerRef.current!);
        noInputTimerRef.current = null;
        handleNoInput();
      }
    }, 1000);
    return () => { if (noInputTimerRef.current) { window.clearInterval(noInputTimerRef.current); noInputTimerRef.current = null; } };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [callState]);

  const handleNoInput = () => {
    if (!rePromptedRef.current) {
      rePromptedRef.current = true;
      logEvent('NO_INPUT_1 — 5s silence, re-prompting', 'warn');
      addMsg({ kind: 'meta', text: 'No speech detected for 5s — NO_INPUT_1 logged, timer reset' });
      setCallState('bot_speaking');
      schedule(() => {
        addMsg({ kind: 'bot', text: scenario.rePrompt, tone: 'neutral', latency: randLatency() });
        schedule(() => setCallState('listening'), getSpeechDuration(scenario.rePrompt));
      }, 600);
    } else {
      // Item 4: transition to bot_speaking so status/chips reflect correctly
      logEvent('NO_INPUT_2 — closing politely, outcome customer_unresponsive', 'warn');
      setCallState('bot_speaking');
      addMsg({ kind: 'bot', text: scenario.politeClose, tone: 'empathetic', latency: randLatency() });
      addMsg({ kind: 'meta', text: 'Outcome: customer_unresponsive · CRM flagged for retry scheduling' });
      outcomeRef.current = { label: 'Customer Unresponsive', status: 'gray' };
      schedule(() => endCall(), 1800);
    }
  };

  // Backend outcome tag → call-history label + status colour.
  const BACKEND_OUTCOME: Record<string, { label: string; status: CallHistory['status'] }> = {
    escalated: { label: 'Escalated to Agent', status: 'orange' },
    converted: { label: 'Completed — Interested', status: 'green' },
    declined: { label: 'Declined', status: 'red' },
    customer_unresponsive: { label: 'Customer Unresponsive', status: 'gray' },
    call_dropped: { label: 'Call Dropped', status: 'gray' },
    unsupported_language: { label: 'Unsupported Language', status: 'orange' },
    in_progress: { label: 'Ended by Operator', status: 'gray' },
  };

  // --- live session lifecycle (real VB_Svc over WebSocket) ---
  const startLiveCall = async () => {
    clearAllScheduled();
    endedRef.current = false;
    rePromptedRef.current = false;
    firstUserMsgRef.current = false;
    outcomeRef.current = { label: 'Completed', status: 'green' };
    callStartTimeRef.current = Date.now();
    setTranscript([]);
    setEvents([]);
    setDuration(0);
    setCallState('connecting');
    logEvent('WebSocket handshake — short-lived JWT presented, upgrading…');

    try {
      const result = await apiStartSession(token!, bot.backendId!, bot.backendCustomerId ?? null);
      liveCrmRef.current = result.crm_context || {};
      setSessionId(result.session_id);
      logEvent(`Session ${result.session_id.slice(0, 8)} created · CRM record loaded · use_case=${result.use_case}`, 'ok');

      const handlers = {
        onOpen: () => logEvent('WebSocket upgraded — JWT accepted by VB_Svc', 'ok'),
        onCallState: (status) => setCallState(status as CallState),
        onTranscript: (speaker, text, d) =>
          addMsg({ kind: speaker === 'bot' ? 'bot' : 'user', text, tone: d.tone as Tone | undefined }),
        onAudio: (d) => { void enqueueAudio(d.audio_b64 as string | undefined); },
        onFiller: (text) => {
          addMsg({ kind: 'filler', text });
          logEvent('NLP first token >300ms — pre-rendered filler injected (latency masking)', 'warn');
        },
        onTtsKill: (reason) => {
          flushPlayback();  // halt in-flight bot audio immediately (barge-in)
          setTranscript(prev => {
            const copy = [...prev];
            for (let i = copy.length - 1; i >= 0; i--) {
              if (copy[i].kind === 'bot') { copy[i] = { ...copy[i], interrupted: true }; break; }
            }
            return copy;
          });
          addMsg({ kind: 'meta', text: `Gate 2: ${reason} → TTS_KILL issued · playback buffer flushed` });
          logEvent('BARGE_IN confirmed — TTS halted (<150ms target)', 'warn');
        },
        onReconnected: (text) => {
          addMsg({ kind: 'meta', text: 'Reconnected with same Session ID — state restored from Redis' });
          logEvent('Reconnect inside grace window — session re-bound', 'ok');
          if (text) addMsg({ kind: 'bot', text, tone: 'neutral' });
        },
        onError: (code, message) => logEvent(`Server error ${code}: ${message}`, 'warn'),
        onEnded: (outcome) => {
          outcomeRef.current = BACKEND_OUTCOME[outcome] || { label: 'Completed', status: 'green' };
          logEvent(`Outcome=${outcome} · transcript + metadata persisted to PostgreSQL`, 'ok');
          endCall();
        },
        onSocketClose: () => { if (!endedRef.current) logEvent('Socket closed', 'info'); },
      };
      const makeCall = () => {
        const call = new LiveCall(result.session_id, result.ws_token, handlers);
        liveCallRef.current = call;
        call.connect();
      };
      // Reconnect path for the grace-TTL demo: re-bind the same session id.
      liveReconnectRef.current = makeCall;
      makeCall();
    } catch (err) {
      const msg = err instanceof KoelApiError ? `${err.code}: ${err.message}` : 'connection failed';
      addMsg({ kind: 'meta', text: `Session refused — ${msg}` });
      logEvent(`Session start failed — ${msg}`, 'warn');
      outcomeRef.current = { label: 'Not Connected', status: 'red' };
      endCall();
    }
  };

  // --- session lifecycle ---
  const startCall = () => {
    if (liveCall) { void startLiveCall(); return; }
    clearAllScheduled();
    endedRef.current = false;
    rePromptedRef.current = false;
    lastFillerRef.current = -1;
    firstUserMsgRef.current = false;
    noReconnectRef.current = false;
    outcomeRef.current = { label: 'Completed', status: 'green' };
    callStartTimeRef.current = Date.now();
    const sid = `SES-${Math.random().toString(36).slice(2, 8).toUpperCase()}`;
    setSessionId(sid);
    setTranscript([]);
    setEvents([]);
    setDuration(0);
    setCallState('connecting');
    logEvent('WebSocket handshake — JWT presented, upgrading…');
    // Item 19: pre-dial compliance checks
    logEvent(`Pre-dial checks: ${new Date().toLocaleTimeString('en-IN', { hour12: false })} IST within RBI permitted window (09:00–21:00) · Number not on TRAI DND registry`, 'ok');

    schedule(() => {
      logEvent(`Session ${sid} bound · Redis state created · CRM record loaded`, 'ok');
      logEvent('Ambient audio layer mixed under TTS output');
      setCallState('bot_speaking');
      schedule(() => {
        // Item 27: compute total from components
        const ttsVal = 170 + Math.floor(Math.random() * 40);
        addMsg({ kind: 'bot', text: scenario.greeting, tone: scenario.greetingTone, latency: { stt: 0, nlp: 0, tts: ttsVal, total: ttsVal } });
        logEvent('Greeting synthesised from CRM-personalised template');
        schedule(() => setCallState('listening'), getSpeechDuration(scenario.greeting));
      }, 1100);
    }, 1200);
  };

  const endCall = () => {
    if (endedRef.current) return;
    endedRef.current = true;
    demoActiveRef.current = false;
    setDemoStepTooltip(null);
    clearAllScheduled();
    if (liveCallRef.current) { liveCallRef.current.close(); liveCallRef.current = null; }
    if (soundEnabled && window.speechSynthesis) window.speechSynthesis.cancel();
    setCallState('ended');
    if (!liveCall) logEvent('Session flushed from Redis · transcript + outcome persisted to PostgreSQL', 'ok');

    const finalTranscript = transcriptRef.current;
    const dur = durationRef.current;
    let { label: outcome, status } = outcomeRef.current;
    // Item 25: fix outcome for manual hang-ups
    if (outcome === 'Completed') {
      const userMsgs = finalTranscript.filter(m => m.kind === 'user').length;
      if (userMsgs === 0) { outcome = 'Customer Hung Up'; status = 'red'; }
      else { outcome = 'Ended by Operator'; status = 'gray'; }
    }

    const liveNow = new Date();
    const lats = finalTranscript.filter(m => m.kind === 'bot' && m.latency).map(m => m.latency!.total);
    const avgLat = lats.length ? Math.round(lats.reduce((a, b) => a + b, 0) / lats.length) : 0;

    onCallCompleted({
      // Item 46: unique call IDs
      id: `CALL-${Date.now().toString(36).toUpperCase()}`,
      bot: bot.name,
      language: bot.language,
      durationStr: `${Math.floor(dur / 60)}m ${(dur % 60).toString().padStart(2, '0')}s`,
      durationSec: dur,
      outcome,
      dateStr: liveNow.toLocaleDateString('en-IN', { day: '2-digit', month: 'short' }) + ', ' + liveNow.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', hour12: false }),
      timestamp: liveNow.getTime(),
      status,
      customer: customer.name,
      customerId: customer.customerId, // Item 16
      phone: customer.phone,
      avgLatencyMs: avgLat,
      transcript: finalTranscript,
      events: [...events], // Item 35
    });
  };

  // --- core turn engine ---
  const runBotTurn = (chip: ChipDef) => {
    setCallState('processing');
    logEvent('Final transcript → NLP_svc (Groq) · intent classification + RAG');

    const lat = randLatency(chip.nlpSlow);

    if (chip.nlpSlow) {
      schedule(() => {
        const pool = FILLER_POOLS[bot.language] || FILLER_POOLS.English;
        const idx = (lastFillerRef.current + 1) % pool.length;
        lastFillerRef.current = idx;
        addMsg({ kind: 'filler', text: pool[idx] });
        logEvent(`NLP first token >300ms — filler clip ${idx + 1}/${pool.length} (${bot.language}) injected, first byte 84ms`, 'warn');
      }, 450);
    }

    schedule(() => {
      if (chip.ragContext) addMsg({ kind: 'meta', text: chip.ragContext });
      // Item 17: IRDAI consent capture
      if (chip.consentCapture) {
        addMsg({ kind: 'meta', text: `${chip.consentCapture} · ${new Date().toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })}` });
        logEvent('IRDAI verbal consent recorded with timestamp', 'ok');
      }
      setCallState(chip.action === 'ESCALATE' ? 'escalating' : 'bot_speaking');
      addMsg({ kind: 'bot', text: chip.botReply, tone: chip.tone, latency: lat });
      logEvent(`Turn complete — STT ${lat.stt}ms · NLP ${lat.nlp}ms · TTS ${lat.tts}ms · total ${lat.total}ms`, lat.total <= 300 ? 'ok' : 'warn');

      if (chip.outcome) outcomeRef.current = chip.outcome;

      if (chip.action === 'ESCALATE') {
        // Item 12: mute ambient on escalation
        logEvent('Ambient audio bed muted for agent handoff', 'info');
        schedule(() => {
          addMsg({ kind: 'meta', text: 'Escalation packet dispatched: full transcript + CRM context + intent history → agent queue' });
          logEvent('Escalation handoff — 100% context, no repeat authentication', 'ok');
          schedule(() => endCall(), 1600);
        }, 1800);
      } else if (chip.action === 'END_POLITE') {
        schedule(() => endCall(), getSpeechDuration(chip.botReply));
      } else {
        schedule(() => setCallState('listening'), getSpeechDuration(chip.botReply));
      }
    }, chip.nlpSlow ? 2100 : 1100);
  };

  const simulateUserResponse = (chip: ChipDef) => {
    if (callState !== 'listening') return;
    rePromptedRef.current = false;
    setCallState('user_speaking');
    addMsg({ kind: 'user', text: chip.userText });

    // Item 14: first-utterance language detection
    if (!firstUserMsgRef.current) {
      firstUserMsgRef.current = true;
      const langCode = LANG_CODES[bot.language] || 'en';
      logEvent(`STT language_id=${langCode} (${bot.language}) · confidence 0.96 — language locked for session`, 'ok');
      // Item 18: DPDP consent at call open
      addMsg({ kind: 'meta', text: `DPDP consent captured at call open · ${new Date().toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })}` });
      logEvent('DPDP consent captured — customer affirmed at call open', 'ok');
    }

    logEvent('Customer audio streaming → STT_svc (Whisper) · partial transcripts');
    schedule(() => runBotTurn(chip), 900);
  };

  // --- resilience edge cases (PRD Section 5) ---
  const simulateBackchannel = () => {
    if (callState !== 'bot_speaking') return;
    addMsg({ kind: 'meta', text: 'Gate 1: VAD flagged BARGE_IN_CANDIDATE → Gate 2: "Hmm" matched backchannel dictionary — ignored, TTS continues' });
    logEvent('Backchannel "Hmm" — dual-gate ignored, no TTS interruption', 'ok');
  };

  const simulateBargeIn = () => {
    if (callState !== 'bot_speaking') return;
    clearAllScheduled();
    // Item 23: reset outcomeRef when barge-in cancels terminal close
    outcomeRef.current = { label: 'Completed', status: 'green' };
    setTranscript(prev => {
      const copy = [...prev];
      for (let i = copy.length - 1; i >= 0; i--) {
        if (copy[i].kind === 'bot') { copy[i] = { ...copy[i], interrupted: true }; break; }
      }
      return copy;
    });
    // Item 32: show Gate 1 before Gate 2
    addMsg({ kind: 'meta', text: 'Gate 1: VAD flagged BARGE_IN_CANDIDATE — forwarding to Gate 2' });
    addMsg({ kind: 'meta', text: 'Gate 2: sustained non-backchannel speech → TTS_KILL issued · playback buffer flushed in 132ms' });
    logEvent('BARGE_IN confirmed — TTS halted in 132ms (<150ms target)', 'warn');
    if (soundEnabled && window.speechSynthesis) window.speechSynthesis.cancel();
    setCallState('user_speaking');
    addMsg({ kind: 'user', text: scenario.bargeInUserText });
    schedule(() => runBotTurn({
      label: '', userText: '', botReply: scenario.bargeInReply, tone: 'empathetic',
      ragContext: 'Context note: previous bot turn marked [interrupted] — LLM knows what the customer actually heard'
    }), 900);
  };

  const simulateUnsupportedLanguage = () => {
    if (callState !== 'listening') return;
    rePromptedRef.current = false;
    setCallState('user_speaking');
    addMsg({ kind: 'user', text: 'मला हे समजत नाही, मराठीत बोला ना…' });
    logEvent('STT language_id=mr (Marathi) · confidence 0.94 — unsupported', 'warn');
    schedule(() => {
      setCallState('processing');
      schedule(() => {
        addMsg({ kind: 'meta', text: 'Graceful language fallback — no hallucinated translation. Session tagged unsupported_language' });
        setCallState('escalating');
        logEvent('Ambient audio bed muted for agent handoff', 'info');
        addMsg({ kind: 'bot', tone: 'empathetic', latency: randLatency(), text: 'Maaf kijiye, main keval Hindi ya English samajh sakti hoon. Main aapko ek agent se connect karti hoon.' });
        outcomeRef.current = { label: 'Unsupported Language', status: 'orange' };
        schedule(() => {
          addMsg({ kind: 'meta', text: 'Escalated to multilingual agent queue with full context' });
          schedule(() => endCall(), 1400);
        }, 1800);
      }, 900);
    }, 800);
  };

  // Item 2: mid-call language switching
  const simulateLanguageSwitch = () => {
    if (callState !== 'listening') return;
    rePromptedRef.current = false;
    setCallState('user_speaking');
    addMsg({ kind: 'user', text: 'Continue in English please.' });
    logEvent('STT language_id change: hi-Latn → en · confidence 0.93 — session language updated', 'warn');
    addMsg({ kind: 'meta', text: 'Mid-call language switch detected: Hinglish → English · session language updated · TTS voice profile adjusted' });
    schedule(() => {
      setCallState('processing');
      schedule(() => {
        setCallState('bot_speaking');
        const replyText = 'Sure, switching to English. So as I was saying, this offer is specifically tailored to your spending profile. Would you like to hear the details?';
        addMsg({ kind: 'bot', tone: 'cheerful', latency: randLatency(), text: replyText });
        logEvent('Language switch complete — TTS now rendering in English', 'ok');
        schedule(() => setCallState('listening'), getSpeechDuration(replyText));
      }, 1100);
    }, 900);
  };

  // Item 10: background noise doesn't reset no-input timer
  const simulateBackgroundNoise = () => {
    if (callState !== 'listening') return;
    addMsg({ kind: 'meta', text: 'VAD: non-speech energy detected (background noise / cough) — no STT cycle triggered, no-input timer NOT reset' });
    logEvent('Non-speech audio filtered by VAD — no-input countdown continues', 'info');
  };

  const simulateNetworkDrop = () => {
    if (callState === 'idle' || callState === 'ended' || callState === 'reconnecting' || callState === 'connecting') return;
    clearAllScheduled();
    noReconnectRef.current = false;
    setCallState('reconnecting');
    addMsg({ kind: 'meta', text: 'WebSocket dropped (network handoff) · TTS paused · Redis session held on 15s grace TTL' });
    logEvent('Socket disconnect — session frozen, 15s grace TTL armed', 'warn');
    if (soundEnabled && window.speechSynthesis) window.speechSynthesis.cancel();

    let remaining = 15;
    setGraceCountdown(remaining);
    graceIntervalRef.current = window.setInterval(() => {
      remaining -= 1;
      setGraceCountdown(remaining);
      // Item 11: if no-reconnect was triggered, let TTL expire
      if (remaining <= 0) {
        if (graceIntervalRef.current) { window.clearInterval(graceIntervalRef.current); graceIntervalRef.current = null; }
        setGraceCountdown(null);
        addMsg({ kind: 'meta', text: 'No reconnect within 15s grace window — session closed, outcome call_dropped' });
        logEvent('Grace TTL expired — no reconnect, session terminated as call_dropped', 'warn');
        outcomeRef.current = { label: 'Call Dropped', status: 'gray' };
        endCall();
        return;
      }
    }, 1000);

    // Client reconnects after ~4s unless user blocked it
    schedule(() => {
      if (noReconnectRef.current) return; // user chose not to reconnect
      if (graceIntervalRef.current) { window.clearInterval(graceIntervalRef.current); graceIntervalRef.current = null; }
      setGraceCountdown(null);
      addMsg({ kind: 'meta', text: `Reconnected with same Session ID (${sessionId}) — RECONNECTED span emitted, state restored from Redis` });
      logEvent('Reconnect inside grace window — session re-bound to new socket', 'ok');
      setCallState('bot_speaking');
      schedule(() => {
        const reconnectText = 'Sorry, lagta hai line cut ho gayi thi — am I audible? Toh jaisa main keh rahi thi…';
        addMsg({ kind: 'bot', tone: 'neutral', latency: randLatency(), text: reconnectText });
        schedule(() => setCallState('listening'), getSpeechDuration(reconnectText));
      }, 800);
    }, 4000);
  };

  // Item 11: allow grace TTL to expire
  const simulateNoReconnect = () => {
    if (callState !== 'reconnecting') return;
    noReconnectRef.current = true;
    // Cancel the scheduled reconnect
    pendingTimeouts.current.forEach(t => window.clearTimeout(t));
    pendingTimeouts.current = [];
    addMsg({ kind: 'meta', text: 'Reconnect blocked — waiting for 15s grace TTL to expire…' });
    logEvent('No-reconnect path active — grace window counting down to expiry', 'warn');
  };

  // --- live-aware interaction handlers (route to VB_Svc when live, else simulate) ---
  const beLang = bot.language === 'Hindi' ? 'hi' : bot.language === 'English' ? 'en' : 'hinglish';

  const handleChip = (chip: ChipDef) => {
    if (!liveCall) { simulateUserResponse(chip); return; }
    if (callState !== 'listening') return;
    rePromptedRef.current = false;
    liveCallRef.current?.sendText(chip.userText); // backend echoes the transcript
  };

  const handleBargeIn = () => {
    if (!liveCall) { simulateBargeIn(); return; }
    // Gate-1 candidate: sustained, non-backchannel speech → backend issues TTS_KILL.
    liveCallRef.current?.bargeIn('ruko ruko, ek sawaal hai', 1100, beLang);
    schedule(() => liveCallRef.current?.sendText('Ruko, ek sawaal hai'), 700);
  };

  const handleBackchannel = () => {
    if (!liveCall) { simulateBackchannel(); return; }
    // Short backchannel → Gate 2 ignores it (no TTS_KILL expected).
    liveCallRef.current?.bargeIn('achha', 180, beLang);
    logEvent('Backchannel sent — expecting Gate 2 to ignore (no interruption)', 'info');
  };

  const handleUnsupported = () => {
    if (!liveCall) { simulateUnsupportedLanguage(); return; }
    if (callState !== 'listening') return;
    // Marathi greeting token → STT stub flags language_id=other → fallback path.
    liveCallRef.current?.sendText('नमस्कार, मला हिंदी येत नाही');
  };

  const handleLanguageSwitch = () => {
    if (!liveCall) { simulateLanguageSwitch(); return; }
    if (callState !== 'listening') return;
    liveCallRef.current?.sendText('Please continue in English from now on');
  };

  const handleBackgroundNoise = () => {
    if (!liveCall) { simulateBackgroundNoise(); return; }
    // Pure noise never crosses client-side VAD, so nothing is sent to the server.
    addMsg({ kind: 'meta', text: 'VAD: non-speech energy — filtered client-side, no frame sent to VB_Svc' });
    logEvent('Non-speech audio filtered by VAD — no-input timer NOT reset', 'info');
  };

  const handleNetworkDrop = () => {
    if (!liveCall) { simulateNetworkDrop(); return; }
    if (callState === 'idle' || callState === 'ended' || callState === 'connecting' || callState === 'reconnecting') return;
    setCallState('reconnecting');
    addMsg({ kind: 'meta', text: 'WebSocket dropped · VB_Svc holds the session on a 15s Redis grace TTL' });
    logEvent('Socket closed — backend grace TTL armed (15s)', 'warn');
    liveCallRef.current?.close();
    liveCallRef.current = null;
    // Re-bind the same Session ID inside the grace window (real resume).
    schedule(() => { if (!endedRef.current) liveReconnectRef.current?.(); }, 3000);
  };

  return (
    <div className="flex flex-col bg-white dark:bg-gray-900 animate-in fade-in duration-300" style={{ height: 'calc(100vh - 64px)', backgroundColor: theme.surfaceCard }}>
      {/* Top Bar */}
      <div className="flex items-center justify-between px-4 md:px-6 py-3 border-b z-10 shadow-sm" style={{ borderColor: theme.rule, backgroundColor: theme.surface }}>
        <div className="flex items-center gap-4">
          <button onClick={onBack} className="flex items-center px-3 py-1.5 rounded-lg shadow-sm text-sm font-medium transition-all border focus-visible:ring-2" style={{ backgroundColor: theme.surfaceCard, borderColor: theme.rule, color: theme.inkMuted }}>
            <ChevronRight className="rotate-180 mr-1" size={16} /> Exit Sandbox
          </button>
          <div className="h-6 w-px" style={{ backgroundColor: theme.rule }}></div>
          <div>
            <h2 className="font-bold text-lg" style={{ color: theme.ink }}>{bot.name}</h2>
            <p className="text-xs font-medium tracking-wide" style={{ color: theme.inkMuted }}>
              {bot.useCase} • {bot.language}{sessionId && isLive ? <> • <span className="font-mono">{sessionId}</span></> : ''}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-4 md:gap-6">
          {/* Item 52: sound toggle */}
          <button onClick={() => setSoundEnabled(!soundEnabled)} className="hidden md:flex items-center gap-1.5 px-2 py-1 rounded text-xs font-bold transition-colors" style={{ color: soundEnabled ? theme.green : theme.inkMuted }} aria-label={soundEnabled ? 'Disable audio' : 'Enable audio'} title={soundEnabled ? 'Audio On (Speech Synthesis)' : 'Audio Off'}>
            <Volume2 size={14} /> {soundEnabled ? 'Audio On' : 'Audio Off'}
          </button>
          <div className="flex flex-col items-end">
            <span className="text-[10px] font-bold uppercase tracking-widest mb-0.5" style={{ color: theme.inkMuted }} role="status" aria-live="polite">Session Status</span>
            {callState === 'idle' && <span className="font-bold px-2.5 py-0.5 rounded text-sm" style={{ backgroundColor: darkMode ? '#374151' : '#E5E7EB', color: theme.inkMuted }}>Ready</span>}
            {callState === 'connecting' && <span className="text-yellow-600 dark:text-yellow-400 font-bold animate-pulse bg-yellow-50 dark:bg-yellow-900/30 px-2.5 py-0.5 rounded text-sm border border-yellow-200 dark:border-yellow-800">Connecting…</span>}
            {callState === 'reconnecting' && (
              <span className="text-orange-600 dark:text-orange-400 font-bold bg-orange-50 dark:bg-orange-900/30 border border-orange-200 dark:border-orange-800 px-2.5 py-0.5 rounded text-sm flex items-center gap-1.5 animate-pulse">
                <WifiOff size={13} /> Reconnecting{graceCountdown !== null ? ` · TTL ${graceCountdown}s` : ''}
              </span>
            )}
            {callState === 'escalating' && (
              <span className="font-bold px-2.5 py-0.5 rounded text-sm flex items-center gap-1.5" style={{ color: theme.accent, backgroundColor: darkMode ? '#7C2D12' + '30' : '#FFF0EB', borderColor: darkMode ? '#9A3412' : '#F5C0AD' }}>
                <Zap size={13} className="animate-pulse" /> Escalating • {formatTime(duration)}
              </span>
            )}
            {/* Item 13: Processing as distinct badge */}
            {callState === 'processing' && (
              <span className="text-yellow-700 dark:text-yellow-400 font-bold bg-yellow-50 dark:bg-yellow-900/30 border border-yellow-200 dark:border-yellow-800 px-2.5 py-0.5 rounded text-sm flex items-center gap-1.5">
                <Zap size={13} className="animate-pulse" /> Processing • {formatTime(duration)}
              </span>
            )}
            {(callState === 'bot_speaking' || callState === 'listening' || callState === 'user_speaking') && (
              <span className="text-green-700 dark:text-green-400 font-bold flex items-center gap-2 bg-green-50 dark:bg-green-900/30 border border-green-200 dark:border-green-800 px-2.5 py-0.5 rounded text-sm">
                <span className="w-2 h-2 rounded-full bg-green-500 animate-ping"></span>Live • {formatTime(duration)}
              </span>
            )}
            {callState === 'ended' && <span className="text-red-600 dark:text-red-400 font-bold bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 px-2.5 py-0.5 rounded text-sm">Ended • {formatTime(duration)}</span>}
          </div>
          {callState === 'idle' || callState === 'ended' ? (
            <button onClick={startCall} className="px-4 md:px-6 py-2.5 rounded-lg text-white font-bold flex items-center gap-2 shadow-md transition-transform hover:scale-105 focus-visible:ring-2" style={{ backgroundColor: theme.primary }}>
              <PhoneCall size={18} /> {callState === 'ended' ? 'Restart' : 'Start Call'}
            </button>
          ) : (
            <button onClick={endCall} className="px-4 md:px-6 py-2.5 rounded-lg bg-red-600 hover:bg-red-700 text-white font-bold flex items-center gap-2 shadow-md transition-transform hover:scale-105 focus-visible:ring-2">
              <Square size={18} fill="currentColor" /> End Call
            </button>
          )}
        </div>
      </div>

      <div className="flex flex-1 overflow-hidden">
        {/* Left sidebar: CRM, past interactions, guardrails, OTel */}
        <div className="hidden lg:flex w-[330px] border-r flex-col shrink-0 overflow-y-auto" style={{ borderColor: theme.rule, backgroundColor: theme.sidebarBg }}>
          {/* CRM Panel */}
          <div className="p-5 border-b" style={{ borderColor: theme.rule }}>
            <div className="flex items-center gap-2 text-[11px] font-bold uppercase tracking-wider mb-3" style={{ color: theme.inkMuted }}>
              <Database size={14} /> CRM Record · Dummy CRM
            </div>
            <div className="rounded-lg p-4 shadow-sm border" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
              <div className="flex items-center gap-3 mb-4 pb-4 border-b" style={{ borderColor: darkMode ? '#1F2937' : '#F3F4F6' }}>
                <div className="w-10 h-10 rounded-full flex items-center justify-center font-bold text-lg" style={{ backgroundColor: theme.primaryLight, color: theme.primary }}>
                  {customer.name.charAt(0)}
                </div>
                <div>
                  <h3 className="font-bold leading-tight" style={{ color: theme.ink }}>{customer.name}</h3>
                  <p className="text-[11px] font-mono mt-0.5" style={{ color: theme.inkMuted }}>{customer.customerId} · {customer.phone}</p>
                </div>
              </div>
              <div className="space-y-3 text-sm">
                <div className="flex justify-between items-center"><span className="text-xs" style={{ color: theme.inkMuted }}>Segment</span><span className="font-semibold" style={{ color: theme.ink }}>{customer.segment}</span></div>
                {customer.fields.map(f => (
                  <div key={f.label} className="flex justify-between items-center gap-2">
                    <span className="text-xs" style={{ color: theme.inkMuted }}>{f.label}</span>
                    {f.highlight ? (
                      <span className={`font-semibold px-2 py-0.5 rounded-md text-[11px] uppercase tracking-wide ${f.highlight === 'green' ? 'text-green-700 dark:text-green-400 bg-green-100 dark:bg-green-900/30' : 'text-orange-700 dark:text-orange-400 bg-orange-100 dark:bg-orange-900/30'}`}>{f.value}</span>
                    ) : (
                      <span className="font-semibold text-right" style={{ color: theme.ink }}>{f.value}</span>
                    )}
                  </div>
                ))}
              </div>
              <p className="text-[10px] mt-4 pt-3 border-t leading-relaxed" style={{ color: theme.inkMuted, borderColor: darkMode ? '#1F2937' : '#F3F4F6' }}>
                Only use-case-relevant fields loaded into session (DPDP data minimisation). PII masked in logs.
              </p>
            </div>
          </div>

          {/* Intent Classification — live */}
          <div className="p-5 border-b" style={{ borderColor: theme.rule }}>
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-2 text-[11px] font-bold uppercase tracking-wider" style={{ color: theme.inkMuted }}>
                <Activity size={14} /> Intent Classification
              </div>
              <span className="text-[9px] font-medium" style={{ fontFamily: '"DM Mono", monospace', color: theme.faint }}>live</span>
            </div>
            <IntentPanel callState={callState} theme={theme} />
          </div>

          {/* Escalation Packet — shown during escalation */}
          {callState === 'escalating' && (
            <div className="p-5 border-b animate-in fade-in" style={{ borderColor: theme.rule }}>
              <div className="flex items-center justify-between mb-3">
                <div className="flex items-center gap-2 text-[11px] font-bold uppercase tracking-wider" style={{ color: theme.inkMuted }}>
                  <Zap size={14} /> Escalation Packet
                </div>
                <span className="flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[11px] font-medium" style={{ backgroundColor: theme.accentLight, color: '#B23A14' }}>Preparing</span>
              </div>
              <div className="rounded-lg border overflow-hidden" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
                {[
                  ['Target queue', 'Sales specialist'],
                  ['Transcript', 'Attached'],
                  ['CRM context', 'Attached'],
                  ['Re-authentication', 'Not required'],
                ].map(([k, v]) => (
                  <div key={k} className="flex justify-between items-center px-4 py-2.5 border-b last:border-b-0 text-[13px]" style={{ borderColor: theme.ruleSubtle }}>
                    <span style={{ color: theme.inkMuted }}>{k}</span>
                    <span className="font-medium" style={{ color: v === 'Attached' ? theme.green : theme.ink }}>{v}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Past Interactions */}
          <div className="p-5 border-b" style={{ borderColor: theme.rule }}>
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-2 text-[11px] font-bold uppercase tracking-wider" style={{ color: theme.inkMuted }}>
                <MessageSquare size={14} /> Past Interactions
              </div>
              <span className="text-[10px] px-2 py-0.5 rounded-full font-bold" style={{ backgroundColor: darkMode ? '#374151' : '#E5E7EB', color: theme.inkMuted }}>{PAST_INTERACTIONS.length} Calls</span>
            </div>
            <div className="flex flex-col gap-2">
              {PAST_INTERACTIONS.map(interaction => (
                <div key={interaction.id} className="border rounded-lg overflow-hidden shadow-sm" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
                  <button onClick={() => setExpandedInteraction(expandedInteraction === interaction.id ? null : interaction.id)} className="w-full text-left p-3 flex justify-between items-center transition-colors hover:opacity-80">
                    <div>
                      <p className="font-semibold text-sm leading-tight" style={{ color: theme.ink }}>{interaction.topic}</p>
                      <p className="text-[11px] mt-1" style={{ color: theme.inkMuted }}>{interaction.date} • {interaction.duration}</p>
                    </div>
                    <ChevronDown size={16} className={`transition-transform duration-200 ${expandedInteraction === interaction.id ? 'rotate-180' : ''}`} style={{ color: theme.inkMuted }} />
                  </button>
                  {expandedInteraction === interaction.id && (
                    <div className="p-3 border-t flex flex-col gap-3" style={{ borderColor: darkMode ? '#1F2937' : '#F3F4F6', backgroundColor: theme.sidebarBg }}>
                      {interaction.transcript.map((m, idx) => (
                        <div key={idx} className="flex gap-2 items-start">
                          <div className={`w-5 h-5 rounded-full flex-shrink-0 flex items-center justify-center text-[9px] font-bold text-white mt-0.5 shadow-sm ${m.speaker === 'bot' ? 'bg-[#053BEE]' : 'bg-gray-400'}`}>
                            {m.speaker === 'bot' ? 'K' : 'U'}
                          </div>
                          <p className="text-xs leading-relaxed" style={{ color: m.speaker === 'bot' ? theme.ink : theme.inkMuted }}>{m.text}</p>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>

          {/* Live guardrails */}
          <div className="p-5 border-b" style={{ borderColor: theme.rule }}>
            <div className="flex items-center gap-2 text-[11px] font-bold uppercase tracking-wider mb-3" style={{ color: theme.inkMuted }}>
              <ShieldAlert size={14} /> Active Guardrails
            </div>
            <div className="flex flex-col gap-2">
              {/* Item 12: mute ambient during escalation */}
              <GuardrailItem active={isLive && callState !== 'escalating'} label={callState === 'escalating' ? 'Ambient Audio Layer (MUTED)' : 'Ambient Audio Layer'} theme={theme} />
              <GuardrailItem active={callState === 'bot_speaking'} label="Dual-Gate Barge-In Engine" pulsing={callState === 'bot_speaking'} theme={theme} />
              <GuardrailItem active={callState === 'listening'} label={`No-Input Watchdog${noInputCountdown !== null ? ` (${noInputCountdown}s)` : ''}`} pulsing={callState === 'listening'} theme={theme} />
              <GuardrailItem active={callState === 'processing'} label="pgvector RAG Retrieval" pulsing={callState === 'processing'} theme={theme} />
              <GuardrailItem active={callState === 'reconnecting'} label="15s Session Recovery TTL" pulsing={callState === 'reconnecting'} theme={theme} />
              <GuardrailItem active={isLive} label="Language ID Confidence Check" theme={theme} />
              <GuardrailItem active={isLive} label="Tone-Tagged TTS Prosody" theme={theme} />
            </div>
          </div>

          {/* OTel event trace */}
          <div className="p-5 flex-1">
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-2 text-[11px] font-bold uppercase tracking-wider" style={{ color: theme.inkMuted }}>
                <Activity size={14} /> Session Events · OTel Trace
              </div>
              {/* Item 43: Kibana link */}
              <span className="text-[9px] font-bold uppercase px-1.5 py-0.5 rounded" style={{ color: theme.primary, backgroundColor: theme.primarySubtle }}>View in Kibana →</span>
            </div>
            <div ref={eventsContainerRef} className="rounded-lg p-3 font-mono text-[10px] leading-relaxed max-h-56 overflow-y-auto flex flex-col gap-1" style={{ backgroundColor: theme.otelBg }}>
              {events.length === 0 && <span className="text-gray-500">— spans will appear when the session starts —</span>}
              {events.map(ev => (
                <div key={ev.id} className="flex gap-2">
                  <span className="text-gray-500 shrink-0">[{ev.t}]</span>
                  <span className={ev.level === 'ok' ? 'text-green-400' : ev.level === 'warn' ? 'text-yellow-400' : 'text-blue-300'}>{ev.label}</span>
                </div>
              ))}
              <div ref={eventsEndRef} />
            </div>
          </div>
        </div>

        {/* Center: transcript + visualizer + simulation deck */}
        <div className="flex-1 flex flex-col relative min-w-0" style={{ backgroundColor: theme.transcriptBg }}>
          <div className="flex-1 p-4 md:p-8 overflow-y-auto flex flex-col gap-5 pb-72 relative" aria-live="polite">
            {transcript.length === 0 && callState === 'idle' && (
              <div className="absolute inset-0 flex flex-col items-center justify-center p-8" style={{ color: theme.inkMuted }}>
                <PhoneCall size={64} className="mb-6 opacity-20" style={{ color: theme.primary }} />
                <p className="text-xl font-bold mb-2" style={{ fontFamily: '"Lora", serif', color: theme.ink }}>Sandbox Ready</p>
                <p className="text-sm text-center max-w-sm mb-6">Click 'Start Call' to establish the authenticated WebSocket session and stream audio through the Whisper → Groq Llama → Orpheus pipeline.</p>
                <button onClick={runGuidedDemo} className="px-6 py-2.5 rounded-full font-bold text-sm text-white shadow-lg hover:shadow-xl transition-all hover:-translate-y-0.5" style={{ background: `linear-gradient(135deg, ${theme.primary}, ${theme.primaryLight})` }}>
                  ▶ Run Guided Demo
                </button>
              </div>
            )}

            {transcript.map(msg => <TranscriptMessage key={msg.id} msg={msg} theme={theme} />)}

            {(callState === 'bot_speaking' || callState === 'processing' || callState === 'user_speaking' || callState === 'reconnecting' || callState === 'escalating') && (
              <div className="flex gap-2 items-center text-xs font-semibold uppercase tracking-wider p-2 px-4 backdrop-blur-sm rounded-full self-center border shadow-sm" style={{ backgroundColor: darkMode ? 'rgba(17,24,39,0.6)' : 'rgba(255,255,255,0.6)', borderColor: theme.rule, color: theme.inkMuted }}>
                {callState === 'processing' && <><Zap size={14} className="text-yellow-500 animate-pulse" /> Orchestrator routing to NLP_svc…</>}
                {callState === 'bot_speaking' && <><Activity size={14} className="animate-pulse" style={{ color: theme.primary }} /> TTS_svc streaming audio…</>}
                {callState === 'user_speaking' && <><Mic size={14} className="text-green-600 animate-pulse" /> STT_svc transcribing…</>}
                {callState === 'reconnecting' && <><WifiOff size={14} className="text-orange-500 animate-pulse" /> Session frozen — awaiting reconnect…</>}
                {callState === 'escalating' && <><Zap size={14} className="animate-pulse" style={{ color: theme.accent }} /> Handing off to agent queue with full context…</>}
              </div>
            )}
            <div ref={transcriptEndRef} />
            {/* Item 42: Agent Handoff Panel overlay */}
            {callState === 'escalating' && <AgentHandoffPanel theme={theme} darkMode={darkMode} />}
          </div>

          <div className="absolute bottom-0 left-0 right-0">
            <div className="h-10 w-full" style={{ background: `linear-gradient(to top, ${theme.transcriptBg}, transparent)` }}></div>
            <div className="border-t shadow-[0_-10px_40px_rgba(0,0,0,0.05)]" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
              {/* Item 41: larger centered visualizer */}
              <div className="h-24 md:h-28 flex items-center justify-center border-b gap-8" style={{ borderColor: theme.surface }}>
                <CircularVisualizer state={callState} theme={theme} analyser={analyserRef} />
              </div>

              {/* Simulation deck */}
              <div className="p-3 md:p-4" style={{ backgroundColor: theme.surface }}>
                <div className="flex items-center gap-3 overflow-x-auto pb-1">
                  <span className="text-[11px] font-bold uppercase tracking-widest whitespace-nowrap px-3 py-1.5 rounded-md border" style={{ color: theme.inkMuted, backgroundColor: theme.surfaceCard, borderColor: theme.rule }}>Customer Replies</span>
                  {liveCall && micSupported() && (
                    <button
                      onClick={toggleMic}
                      title={micOn ? 'Stop microphone' : 'Speak to the bot (real mic → Whisper STT)'}
                      aria-pressed={micOn}
                      className="flex items-center gap-1.5 px-3 py-2 rounded-full border text-sm font-semibold whitespace-nowrap transition-all shadow-sm focus-visible:ring-2"
                      style={micOn
                        ? { backgroundColor: theme.accent, borderColor: theme.accent, color: '#fff' }
                        : { backgroundColor: theme.surfaceCard, borderColor: theme.rule, color: theme.ink }}
                    >
                      <Mic size={14} className={micOn ? 'animate-pulse' : ''} />
                      {micOn ? 'Listening…' : 'Mic'}
                    </button>
                  )}
                  {callState === 'listening' ? (
                    scenario.chips.map((chip, idx) => {
                      const isDemoTarget = demoStepTooltip?.id === `chip-${idx}`;
                      return (
                        <div key={chip.label} className="relative">
                          <button onClick={() => handleChip(chip)} className={`px-4 py-2 rounded-full border text-sm font-medium transition-all shadow-sm whitespace-nowrap focus-visible:ring-2 ${isDemoTarget ? 'ring-2 ring-offset-2 ring-blue-500 scale-105 z-10' : ''}`} style={{ backgroundColor: theme.surfaceCard, borderColor: theme.rule, color: theme.ink }}>
                            {chip.label}
                          </button>
                          {isDemoTarget && (
                            <div className="absolute bottom-full mb-2 left-1/2 -translate-x-1/2 bg-blue-600 text-white text-[10px] font-bold px-2 py-1 rounded whitespace-nowrap animate-in slide-in-from-bottom-2 z-20 shadow-lg">
                              {demoStepTooltip.text}
                              <div className="absolute top-full left-1/2 -translate-x-1/2 border-[3px] border-transparent border-t-blue-600"></div>
                            </div>
                          )}
                        </div>
                      );
                    })
                  ) : (
                    <span className="text-sm italic px-2 whitespace-nowrap" style={{ color: theme.inkMuted }}>
                      {callState === 'bot_speaking' ? 'Bot is speaking — try a barge-in or backchannel →' : 'Waiting for interaction window…'}
                    </span>
                  )}
                </div>
                <div className="flex items-center gap-3 overflow-x-auto pt-2">
                  <span className="text-[11px] font-bold uppercase tracking-widest whitespace-nowrap px-3 py-1.5 rounded-md border" style={{ color: theme.accent, backgroundColor: darkMode ? '#7C2D12' + '20' : '#FFF0EB', borderColor: darkMode ? '#9A3412' : '#F5C0AD' }}>Edge Cases</span>
                  <EdgeChip icon={<Square size={12} fill="currentColor" />} label="Barge-In (interrupt)" enabled={callState === 'bot_speaking'} onClick={handleBargeIn} title="Customer talks over the bot — Gate 2 passes, TTS_KILL within 150ms" theme={theme} demoTooltip={demoStepTooltip?.id === 'barge-in' ? demoStepTooltip.text : null} />
                  <EdgeChip icon={<Volume2 size={12} />} label={'Backchannel "Hmm"'} enabled={callState === 'bot_speaking'} onClick={handleBackchannel} title="Listening sound — Gate 2 ignores it, TTS continues" theme={theme} />
                  <EdgeChip icon={<Languages size={12} />} label="Speak Marathi" enabled={callState === 'listening'} onClick={handleUnsupported} title="Unsupported language → graceful fallback + agent escalation" theme={theme} />
                  {/* Item 2: language switch chip */}
                  <EdgeChip icon={<Languages size={12} />} label="Switch to English" enabled={callState === 'listening'} onClick={handleLanguageSwitch} title="Mid-call language switch — STT detects change, TTS adjusts" theme={theme} demoTooltip={demoStepTooltip?.id === 'lang-switch' ? demoStepTooltip.text : null} />
                  {/* Item 10: background noise chip */}
                  <EdgeChip icon={<Mic size={12} />} label="Background Noise" enabled={callState === 'listening'} onClick={handleBackgroundNoise} title="Non-speech audio — VAD filters it, no-input timer NOT reset" theme={theme} demoTooltip={demoStepTooltip?.id === 'noise' ? demoStepTooltip.text : null} />
                  {/* Item 45: disable Drop during connecting */}
                  <EdgeChip icon={<WifiOff size={12} />} label="Drop Network" enabled={isLive && callState !== 'reconnecting' && callState !== 'connecting'} onClick={handleNetworkDrop} title="Socket drop → 15s Redis grace TTL → resume with same Session ID" theme={theme} />
                  {/* Item 11: no-reconnect chip */}
                  <EdgeChip icon={<Timer size={12} />} label="Drop (no reconnect)" enabled={callState === 'reconnecting'} onClick={simulateNoReconnect} title="Block reconnect — let 15s grace TTL expire → call_dropped" theme={theme} />
                  <EdgeChip icon={<Timer size={12} />} label="Stay Silent" enabled={callState === 'listening'} onClick={() => { }} title="Just wait — 5s silence triggers a re-prompt, 5 more closes the call" passive theme={theme} />
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}


// ============================================================================
// Simulation Chips
// ============================================================================
function EdgeChip({ icon, label, enabled, onClick, title, theme, passive, demoTooltip }: { icon: React.ReactNode; label: string; enabled: boolean; onClick: () => void; title: string; theme: ReturnType<typeof getTheme>; passive?: boolean; demoTooltip?: string | null }) {
  const isDemoTarget = !!demoTooltip;
  return (
    <div className="relative">
      <button title={title} onClick={onClick} disabled={!enabled} className={`flex items-center gap-1.5 px-3 py-1.5 rounded-full border text-xs font-medium transition-all whitespace-nowrap focus-visible:ring-2 ${passive ? 'shadow-none' : 'shadow-sm'} ${!enabled ? 'opacity-40 cursor-not-allowed' : 'hover:-translate-y-px'} ${isDemoTarget ? 'ring-2 ring-offset-2 ring-blue-500 scale-105 z-10' : ''}`} style={{ backgroundColor: theme.surfaceCard, borderColor: theme.rule, color: passive ? theme.inkMuted : theme.ink }}>
        <span style={{ color: enabled ? (passive ? theme.inkMuted : theme.ink) : theme.inkMuted }}>{icon}</span> {label}
      </button>
      {isDemoTarget && (
        <div className="absolute bottom-full mb-2 left-1/2 -translate-x-1/2 bg-blue-600 text-white text-[10px] font-bold px-2 py-1 rounded whitespace-nowrap animate-in slide-in-from-bottom-2 z-20 shadow-lg">
          {demoTooltip}
          <div className="absolute top-full left-1/2 -translate-x-1/2 border-[3px] border-transparent border-t-blue-600"></div>
        </div>
      )}
    </div>
  );
}

// ============================================================================
// Transcript bubbles
// ============================================================================
function TranscriptMessage({ msg, compact, theme }: { msg: TranscriptMessageData; compact?: boolean; theme: ReturnType<typeof getTheme> }) {
  if (msg.kind === 'meta') {
    return (
      <div className="flex justify-center my-1 animate-in fade-in slide-in-from-bottom-2">
        <span className="px-4 py-1.5 text-[11px] font-bold uppercase tracking-wider rounded-full flex items-center gap-1.5 border shadow-sm text-center max-w-[85%]" style={{ backgroundColor: theme.primarySubtle, color: theme.primary, borderColor: theme.primaryLight }}>
          <Database size={11} className="shrink-0" /> {msg.text}
        </span>
      </div>
    );
  }

  if (msg.kind === 'filler') {
    return (
      <div className="flex justify-start my-1 animate-in fade-in slide-in-from-bottom-2 ml-11">
        <span className="px-4 py-1.5 bg-[#FFF7E6] dark:bg-[#422006]/40 text-[#9A6700] dark:text-yellow-400 text-xs font-medium italic rounded-full flex items-center gap-1.5 border border-[#F5D789] dark:border-yellow-800 shadow-sm">
          <Volume2 size={12} /> Filler audio: {msg.text}
        </span>
      </div>
    );
  }

  const isBot = msg.kind === 'bot';
  return (
    <div className={`flex flex-col max-w-[80%] animate-in fade-in slide-in-from-bottom-2 ${isBot ? 'self-start items-start' : 'self-end items-end'}`}>
      <span className="text-[10px] uppercase font-bold tracking-wider mb-1 mx-1 flex items-center gap-2" style={{ color: theme.inkMuted }}>
        {isBot ? 'KOEL Bot' : 'Customer'}
        {isBot && msg.tone && (
          <span className={`px-1.5 py-px rounded border text-[9px] normal-case font-bold ${TONE_STYLES[msg.tone]}`}>[{msg.tone}]</span>
        )}
        {msg.interrupted && (
          <span className="px-1.5 py-px rounded border text-[9px] normal-case font-bold bg-red-50 text-red-600 border-red-200">interrupted</span>
        )}
        {/* Item 58: per-turn timestamp */}
        {msg.timestamp !== undefined && (
          <span className="font-mono text-[9px] normal-case" style={{ color: theme.inkMuted }}>{formatTime(msg.timestamp)}</span>
        )}
      </span>
      <div className={`flex gap-3 items-end ${isBot ? '' : 'flex-row-reverse'}`}>
        <div className={`${compact ? 'w-6 h-6 text-[9px]' : 'w-8 h-8 text-xs'} rounded-full flex-shrink-0 flex items-center justify-center font-bold text-white shadow-sm ${isBot ? 'bg-[#053BEE]' : 'bg-gray-400'}`}>
          {isBot ? 'K' : 'U'}
        </div>
        <div className={`${compact ? 'px-4 py-3 text-[14px]' : 'p-4 text-[15px]'} rounded-2xl leading-relaxed shadow-sm border ${msg.interrupted ? 'opacity-70' : ''}`}
          style={{
            backgroundColor: isBot ? theme.surfaceCard : theme.primaryLight,
            color: isBot ? theme.ink : theme.primaryDark,
            borderColor: isBot ? theme.rule : theme.primary + '15',
            borderRadius: isBot ? '1rem 1rem 1rem 0.25rem' : '1rem 1rem 0.25rem 1rem',
          }}>
          {msg.text}
        </div>
      </div>
      {isBot && msg.latency && (
        <span className={`text-[10px] font-mono mt-1 ml-11`} style={{ color: msg.latency.total <= 300 ? theme.inkMuted : '#9A6700' }} title="STT / NLP / TTS / total — from OTel spans">
          ⏱ {msg.latency.stt > 0 ? `stt ${msg.latency.stt} · ` : ''}nlp {msg.latency.nlp} · tts {msg.latency.tts} · <b>{msg.latency.total}ms</b>{msg.latency.total > 300 ? ' (over budget → filler)' : ''}
        </span>
      )}
    </div>
  );
}

function GuardrailItem({ active, label, pulsing, theme }: { active: boolean; label: string; pulsing?: boolean; theme: ReturnType<typeof getTheme> }) {
  return (
    <div className="flex items-center gap-2 text-sm p-2.5 rounded-lg border shadow-sm" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
      {active ? <CheckCircle2 size={16} className={`text-green-600 dark:text-green-400 shrink-0 ${pulsing ? 'animate-pulse' : ''}`} /> : <AlertCircle size={16} className="shrink-0" style={{ color: theme.rule }} />}
      <span className={`text-xs font-medium`} style={{ color: active ? theme.ink : theme.inkMuted }}>{label}</span>
    </div>
  );
}

// ============================================================================
// Items 9, 28, 41: Amplitude-reactive circular waveform visualiser
// ============================================================================
function CircularVisualizer({ state, theme, analyser }: { state: CallState; theme: ReturnType<typeof getTheme>; analyser?: React.RefObject<AnalyserNode | null> }) {
  const config: Record<string, { color: string; text: string; active: boolean }> = {
    idle: { color: '#CBD5E1', text: 'Connection Idle', active: false },
    connecting: { color: '#94A3B8', text: 'Handshaking…', active: true },
    bot_speaking: { color: theme.primary, text: 'KOEL Speaking', active: true },
    user_speaking: { color: '#16A34A', text: 'Receiving Audio', active: true },
    processing: { color: '#EAB308', text: 'Orchestrator Logic', active: true },
    listening: { color: '#7DA2FF', text: 'Listening for Input…', active: true },
    reconnecting: { color: '#F97316', text: 'Session Recovery…', active: true },
    escalating: { color: theme.accent, text: 'Escalating to Agent', active: true },
    ended: { color: '#DC2626', text: 'Session Closed', active: false },
  };
  const { color, text, active } = config[state] || config.idle;
  const bars = 24;
  const svgRef = useRef<SVGSVGElement>(null);
  const amplitudesRef = useRef<number[]>(new Array(bars).fill(0));
  const rafRef = useRef<number>(0);
  const reducedMotion = useRef(typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches);

  useEffect(() => {
    if (reducedMotion.current) return;

    // Per-state amplitude gain — applied to real analyser data when available,
    // otherwise used to drive a graceful fallback animation.
    const stateGain = (): number => {
      if (state === 'bot_speaking' || state === 'user_speaking') return 1;
      if (state === 'escalating') return 0.7;
      if (state === 'processing' || state === 'connecting' || state === 'reconnecting') return 0.5;
      if (state === 'listening') return 0.3;
      return 0.05;
    };

    const freq = new Uint8Array(bars);

    const animate = () => {
      const svg = svgRef.current;
      if (!svg) { rafRef.current = requestAnimationFrame(animate); return; }

      const lines = svg.querySelectorAll('line');
      const amps = amplitudesRef.current;

      // Read real spectrum data off the AnalyserNode when the audio graph is
      // live; fall back to a per-state synthetic shape otherwise.
      const node = analyser?.current ?? null;
      let live = false;
      if (node) {
        try { node.getByteFrequencyData(freq); live = true; } catch { live = false; }
      }
      const gain = stateGain();

      for (let i = 0; i < bars; i++) {
        let target = 0;
        if (live) {
          // 0..255 → bar length, scaled by the call-state gain.
          target = (freq[i] / 255) * 16 * gain + 0.5;
        } else if (state === 'bot_speaking' || state === 'user_speaking') {
          target = 4 + Math.random() * 10;
        } else if (state === 'listening') {
          target = 1 + Math.random() * 3;
        } else if (state === 'processing' || state === 'connecting' || state === 'reconnecting') {
          target = 2 + Math.random() * 5;
        } else if (state === 'escalating') {
          target = 3 + Math.random() * 7;
        } else {
          target = 0.5;
        }
        amps[i] = amps[i] + (target - amps[i]) * 0.15;

        const a = (i / bars) * Math.PI * 2;
        const r1 = 34;
        const r2 = r1 + amps[i];
        const line = lines[i];
        if (line) {
          line.setAttribute('x1', String(40 + r1 * Math.cos(a)));
          line.setAttribute('y1', String(40 + r1 * Math.sin(a)));
          line.setAttribute('x2', String(40 + r2 * Math.cos(a)));
          line.setAttribute('y2', String(40 + r2 * Math.sin(a)));
        }
      }

      rafRef.current = requestAnimationFrame(animate);
    };

    rafRef.current = requestAnimationFrame(animate);
    return () => cancelAnimationFrame(rafRef.current);
  }, [state, bars, analyser]);

  return (
    <div className="flex items-center gap-5">
      {/* Item 41: larger visualizer — 80x80 */}
      <div className="relative w-20 h-20 flex items-center justify-center">
        {active && !reducedMotion.current && (
          <>
            <div className="absolute inset-0 rounded-full animate-ping opacity-15" style={{ backgroundColor: color, animationDuration: '1.6s' }}></div>
            <div className="absolute inset-2 rounded-full animate-pulse opacity-20" style={{ backgroundColor: color }}></div>
          </>
        )}
        <div className="absolute inset-4 rounded-full transition-colors duration-300 flex items-center justify-center" style={{ backgroundColor: color }}>
          <Radio size={20} className="text-white" />
        </div>
        <svg ref={svgRef} viewBox="0 0 80 80" className="absolute inset-0 w-full h-full">
          {Array.from({ length: bars }).map((_, i) => {
            const a = (i / bars) * Math.PI * 2;
            const r1 = 34;
            const r2 = active ? 37 : 35;
            return (
              <line
                key={i}
                x1={40 + r1 * Math.cos(a)} y1={40 + r1 * Math.sin(a)}
                x2={40 + r2 * Math.cos(a)} y2={40 + r2 * Math.sin(a)}
                stroke={color} strokeWidth="2" strokeLinecap="round"
              />
            );
          })}
        </svg>
      </div>
      <div>
        <span className="block text-xs font-bold uppercase tracking-widest" style={{ color: theme.ink }}>{text}</span>
        <span className="block text-[10px] font-medium mt-0.5" style={{ color: theme.inkMuted }}>Web Audio API · amplitude-reactive waveform</span>
      </div>
    </div>
  );
}

// ============================================================================
// Screen 4 — Call History (PRD: filter by bot/date/outcome/language, replay, CSV)
// Items 3, 16, 35, 37, 38, 39, 44, 47, 54, 55
// ============================================================================
function HistoryView({ calls, onExported, theme }: { calls: CallHistory[]; onExported: () => void; theme: ReturnType<typeof getTheme>; darkMode: boolean }) {
  const [selectedCallId, setSelectedCallId] = useState<string | null>(null);
  const [sortBy, setSortBy] = useState<string>('date_desc');
  const [botFilter, setBotFilter] = useState<string>('all');
  const [outcomeFilter, setOutcomeFilter] = useState<string>('all');
  const [languageFilter, setLanguageFilter] = useState<string>('all');
  const [search, setSearch] = useState('');
  // Item 3: date range filter
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [showEvents, setShowEvents] = useState(false); // Item 35: toggle OTel trace in detail
  const [currentPage, setCurrentPage] = useState(1);
  const pageSize = 10;

  const botNames = Array.from(new Set(calls.map(c => c.bot)));
  const languages = Array.from(new Set(calls.map(c => c.language)));
  // Item 37: filter by specific outcome values instead of status buckets
  const outcomeValues = Array.from(new Set(calls.map(c => c.outcome)));

  const filtered = calls.filter(c => {
    if (botFilter !== 'all' && c.bot !== botFilter) return false;
    if (languageFilter !== 'all' && c.language !== languageFilter) return false;
    if (outcomeFilter !== 'all' && c.outcome !== outcomeFilter) return false;
    // Item 3: date filter
    if (dateFrom && c.timestamp < new Date(dateFrom).getTime()) return false;
    if (dateTo && c.timestamp > new Date(dateTo + 'T23:59:59').getTime()) return false;
    if (search && !`${c.id} ${c.customer} ${c.outcome} ${c.customerId || ''}`.toLowerCase().includes(search.toLowerCase())) return false;
    return true;
  });

  // Item 44: clear selection when filtered set excludes it; reset page
  useEffect(() => {
    if (selectedCallId && !filtered.find(c => c.id === selectedCallId)) {
      setSelectedCallId(null);
    }
    setCurrentPage(1);
  }, [filtered.length, selectedCallId]);

  const sortedCalls = [...filtered].sort((a, b) => {
    if (sortBy === 'date_desc') return b.timestamp - a.timestamp;
    if (sortBy === 'date_asc') return a.timestamp - b.timestamp;
    if (sortBy === 'customer') return a.customer.localeCompare(b.customer);
    if (sortBy === 'duration') return b.durationSec - a.durationSec;
    return 0;
  });

  // Item 44: look up from filtered, not all calls
  const selectedCall = filtered.find(c => c.id === selectedCallId);

  // Item 44: dynamic colSpan
  const tableColCount = selectedCallId ? 4 : 8;

  const totalPages = Math.max(1, Math.ceil(sortedCalls.length / pageSize));
  const paginatedCalls = sortedCalls.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  const exportCsv = () => {
    // Item 38: ISO timestamp; Item 16: Customer ID; Item 47: UTF-8 BOM
    const header = ['Session ID', 'Bot', 'Customer', 'Customer ID', 'Phone', 'Language', 'Date', 'Timestamp (ISO)', 'Duration (s)', 'Outcome', 'Avg Latency (ms)'];
    const rows = sortedCalls.map(c => [c.id, c.bot, c.customer, c.customerId || '', c.phone, c.language, c.dateStr, new Date(c.timestamp).toISOString(), String(c.durationSec), c.outcome, String(c.avgLatencyMs)]);
    const csv = '\uFEFF' + [header, ...rows].map(r => r.map(v => `"${v.replace(/"/g, '""')}"`).join(',')).join('\n');
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'koel_call_history.csv';
    a.click();
    URL.revokeObjectURL(url);
    onExported();
  };

  return (
    <div className="max-w-7xl mx-auto p-4 md:p-8 flex flex-col h-full animate-in fade-in duration-300">
      <div className="flex flex-wrap justify-between items-end mb-6 gap-4">
        <div>
          <h1 className="text-3xl font-semibold mb-2" style={{ fontFamily: '"Lora", serif', color: theme.ink }}>Call History</h1>
          <p style={{ color: theme.inkMuted }}>Review transcripts, latencies, and outcomes for all sessions.</p>
        </div>
        <button onClick={exportCsv} className="flex items-center gap-2 px-4 py-2 rounded-lg text-white font-medium transition-opacity hover:opacity-90 shadow-sm text-sm focus-visible:ring-2" style={{ backgroundColor: theme.primary }}>
          <Download size={16} /> Export CSV
        </button>
      </div>

      {/* Filter bar */}
      <div className="flex flex-wrap items-center gap-3 mb-6">
        <div className="flex items-center gap-2 border rounded-lg px-3 py-1.5 shadow-sm" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
          <Search size={15} style={{ color: theme.inkMuted }} />
          <input value={search} onChange={e => setSearch(e.target.value)} placeholder="Search ID, customer, outcome…" className="bg-transparent text-sm font-medium outline-none w-40 md:w-52" style={{ color: theme.ink }} />
        </div>
        <FilterSelect value={botFilter} onChange={setBotFilter} options={[['all', 'All Bots'], ...botNames.map(n => [n, n] as [string, string])]} theme={theme} />
        {/* Item 37: filter by specific outcome value */}
        <FilterSelect value={outcomeFilter} onChange={setOutcomeFilter} options={[['all', 'All Outcomes'], ...outcomeValues.map(o => [o, o] as [string, string])]} theme={theme} />
        <FilterSelect value={languageFilter} onChange={setLanguageFilter} options={[['all', 'All Languages'], ...languages.map(l => [l, l] as [string, string])]} theme={theme} />
        <FilterSelect value={sortBy} onChange={setSortBy} options={[['date_desc', 'Date (Newest)'], ['date_asc', 'Date (Oldest)'], ['customer', 'Customer Name'], ['duration', 'Duration (Longest)']]} theme={theme} />
        {/* Item 3: date range filter */}
        <div className="flex items-center gap-2">
          <input type="date" value={dateFrom} onChange={e => setDateFrom(e.target.value)} className="border rounded-lg px-2 py-1.5 text-xs font-medium outline-none" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard, color: theme.ink }} aria-label="Filter from date" />
          <span className="text-xs" style={{ color: theme.inkMuted }}>to</span>
          <input type="date" value={dateTo} onChange={e => setDateTo(e.target.value)} className="border rounded-lg px-2 py-1.5 text-xs font-medium outline-none" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard, color: theme.ink }} aria-label="Filter to date" />
        </div>
        <span className="text-xs font-medium ml-auto" style={{ color: theme.inkMuted }}>{sortedCalls.length} of {calls.length} sessions</span>
      </div>

      <div className="flex gap-6 flex-1 min-h-[560px] flex-col md:flex-row">
        {/* Call table */}
        <div className={`rounded-lg border flex flex-col transition-all duration-300 shadow-sm ${selectedCallId ? 'md:w-1/2' : 'w-full'}`} style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
          <div className="overflow-y-auto flex-1">
            <table className="w-full text-left border-collapse">
              <thead className="sticky top-0 z-10 shadow-sm" style={{ backgroundColor: theme.surface }}>
                <tr className="border-b text-[11px] uppercase tracking-wider font-bold" style={{ borderColor: theme.rule, color: theme.inkMuted }}>
                  <th className="p-4">Session ID</th>
                  <th className="p-4">Customer</th>
                  {!selectedCallId && <th className="p-4">Bot Instance</th>}
                  {!selectedCallId && <th className="p-4">Language</th>}
                  {!selectedCallId && <th className="p-4">Date</th>}
                  {!selectedCallId && <th className="p-4">Duration</th>}
                  <th className="p-4">Outcome</th>
                  <th className="p-4 text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="text-sm">
                {paginatedCalls.length === 0 && (
                  <tr><td colSpan={tableColCount} className="p-10 text-center text-sm" style={{ color: theme.inkMuted }}>No sessions match the current filters.</td></tr>
                )}
                {paginatedCalls.map(call => (
                  <tr
                    key={call.id}
                    onClick={() => setSelectedCallId(call.id)}
                    onKeyDown={e => (e.key === 'Enter' || e.key === ' ') && setSelectedCallId(call.id)}
                    tabIndex={0}
                    role="button"
                    className={`border-b cursor-pointer transition-colors group focus-visible:ring-2 focus-visible:ring-inset ${selectedCallId === call.id ? 'border-l-4' : 'border-l-4 border-l-transparent'}`}
                    style={{
                      borderColor: theme.rule,
                      borderLeftColor: selectedCallId === call.id ? theme.primary : 'transparent',
                      backgroundColor: selectedCallId === call.id ? theme.primarySubtle : 'transparent',
                    }}
                  >
                    <td className="p-4 font-mono font-medium text-xs" style={{ color: theme.ink }}>{call.id}</td>
                    {/* Item 16: show customer ID */}
                    <td className="p-4">
                      <span className="font-semibold" style={{ color: theme.ink }}>{call.customer}</span>
                      {call.customerId && <span className="block text-[10px] font-mono" style={{ color: theme.inkMuted }}>{call.customerId}</span>}
                    </td>
                    {!selectedCallId && <td className="p-4" style={{ color: theme.inkMuted }}>{call.bot}</td>}
                    {!selectedCallId && <td className="p-4 text-xs" style={{ color: theme.inkMuted }}>{call.language}</td>}
                    {!selectedCallId && <td className="p-4 text-xs" style={{ color: theme.inkMuted }}>{call.dateStr}</td>}
                    {!selectedCallId && <td className="p-4 font-mono text-xs" style={{ color: theme.inkMuted }}>{call.durationStr}</td>}
                    <td className="p-4">
                      <span className={`inline-flex px-2.5 py-1 rounded-md text-[11px] uppercase tracking-wide font-bold
                        ${call.status === 'green' ? 'bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400' : ''}
                        ${call.status === 'orange' ? 'bg-orange-100 dark:bg-orange-900/30 text-orange-700 dark:text-orange-400' : ''}
                        ${call.status === 'red' ? 'bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-400' : ''}
                        ${call.status === 'gray' ? 'bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400' : ''}
                      `}>
                        {call.outcome}
                      </span>
                    </td>
                    <td className="p-4 text-right">
                      {/* Item 54: visible on focus */}
                      <button className={`text-sm font-bold flex items-center justify-end w-full gap-1 transition-opacity focus-visible:opacity-100 ${selectedCallId === call.id ? '' : 'opacity-0 group-hover:opacity-100 group-focus-within:opacity-100'}`} style={{ color: theme.primary }}>
                        {selectedCallId === call.id ? 'Viewing' : 'View'} <ChevronRight size={14} className={selectedCallId === call.id ? 'hidden' : 'block'} />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {/* Pagination */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between px-4 py-3 border-t text-[12.5px]" style={{ borderColor: theme.ruleSubtle, color: theme.inkMuted }}>
              <span>Showing {(currentPage - 1) * pageSize + 1}–{Math.min(currentPage * pageSize, sortedCalls.length)} of {sortedCalls.length}</span>
              <div className="flex gap-1">
                {Array.from({ length: Math.min(totalPages, 5) }, (_, i) => {
                  const pg = totalPages <= 5 ? i + 1 : currentPage <= 3 ? i + 1 : currentPage >= totalPages - 2 ? totalPages - 4 + i : currentPage - 2 + i;
                  return (
                    <button key={pg} onClick={() => setCurrentPage(pg)} className="w-[26px] h-[26px] flex items-center justify-center rounded-md text-[12.5px]" style={{ backgroundColor: currentPage === pg ? theme.primaryLight : 'transparent', color: currentPage === pg ? theme.primary : theme.inkMuted, fontWeight: currentPage === pg ? 500 : 400 }}>
                      {pg}
                    </button>
                  );
                })}
                {totalPages > 5 && <span className="w-[26px] h-[26px] flex items-center justify-center">…</span>}
                {totalPages > 5 && (
                  <button onClick={() => setCurrentPage(totalPages)} className="w-[26px] h-[26px] flex items-center justify-center rounded-md text-[12.5px]" style={{ color: currentPage === totalPages ? theme.primary : theme.inkMuted, backgroundColor: currentPage === totalPages ? theme.primaryLight : 'transparent' }}>
                    {totalPages}
                  </button>
                )}
              </div>
            </div>
          )}
        </div>

        {/* Right: transcript + replay */}
        {selectedCall && (
          <div className="md:w-1/2 rounded-lg border flex flex-col overflow-hidden shadow-md animate-in fade-in slide-in-from-right-4" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
            <div className="p-5 border-b flex justify-between items-start" style={{ borderColor: theme.rule, backgroundColor: theme.sidebarBg }}>
              <div>
                <h3 className="font-bold text-lg mb-1" style={{ color: theme.ink }}>Session Transcript</h3>
                <div className="flex flex-wrap gap-3 text-xs font-medium" style={{ color: theme.inkMuted }}>
                  <span className="px-2 py-1 rounded border shadow-sm font-mono" style={{ backgroundColor: theme.surfaceCard, borderColor: theme.rule }}>{selectedCall.id}</span>
                  <span className="flex items-center gap-1"><User size={12} /> {selectedCall.customer} ({selectedCall.phone})</span>
                  {/* Item 16: Customer ID */}
                  {selectedCall.customerId && <span className="font-mono">{selectedCall.customerId}</span>}
                  <span className="flex items-center gap-1"><Clock size={12} /> {selectedCall.dateStr}</span>
                  <span className="flex items-center gap-1"><Languages size={12} /> {selectedCall.language}</span>
                </div>
              </div>
              <button onClick={() => setSelectedCallId(null)} className="p-1.5 rounded-md transition-colors border shadow-sm focus-visible:ring-2" style={{ backgroundColor: theme.surfaceCard, borderColor: theme.rule, color: theme.inkMuted }} aria-label="Close transcript panel">
                <X size={16} />
              </button>
            </div>

            <div className="flex-1 overflow-y-auto p-6 flex flex-col gap-5" style={{ backgroundColor: theme.transcriptBg }}>
              {selectedCall.transcript.map(msg => (
                <TranscriptMessage key={msg.id} msg={msg} compact theme={theme} />
              ))}
              {selectedCall.transcript.length === 0 && (
                <p className="text-sm italic text-center mt-10" style={{ color: theme.inkMuted }}>No utterances captured before the session ended.</p>
              )}
            </div>

            {/* Item 35: OTel trace in history detail */}
            {selectedCall.events && selectedCall.events.length > 0 && (
              <div className="border-t" style={{ borderColor: theme.rule }}>
                <button onClick={() => setShowEvents(!showEvents)} className="w-full p-3 flex items-center justify-between text-xs font-bold uppercase tracking-wider" style={{ color: theme.inkMuted, backgroundColor: theme.surface }}>
                  <span className="flex items-center gap-2"><Activity size={12} /> Session Events ({selectedCall.events.length})</span>
                  <ChevronDown size={14} className={`transition-transform ${showEvents ? 'rotate-180' : ''}`} />
                </button>
                {showEvents && (
                  <div className="p-3 font-mono text-[10px] leading-relaxed max-h-40 overflow-y-auto flex flex-col gap-1" style={{ backgroundColor: theme.otelBg }}>
                    {selectedCall.events.map(ev => (
                      <div key={ev.id} className="flex gap-2">
                        <span className="text-gray-500 shrink-0">[{ev.t}]</span>
                        <span className={ev.level === 'ok' ? 'text-green-400' : ev.level === 'warn' ? 'text-yellow-400' : 'text-blue-300'}>{ev.label}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            <AudioReplayBar call={selectedCall} theme={theme} />
          </div>
        )}
      </div>
    </div>
  );
}

function FilterSelect({ value, onChange, options, theme }: { value: string; onChange: (v: string) => void; options: [string, string][]; theme: ReturnType<typeof getTheme> }) {
  return (
    <div className="flex items-center gap-2 border rounded-lg px-3 py-1.5 shadow-sm" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
      <Filter size={14} style={{ color: theme.inkMuted }} />
      <select className="bg-transparent text-sm font-medium outline-none cursor-pointer" style={{ color: theme.ink }} value={value} onChange={e => onChange(e.target.value)}>
        {options.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
      </select>
    </div>
  );
}

// Item 26: fix replay pacing; Item 39: data residency; Item 49: pure updaters
function AudioReplayBar({ call, theme }: { call: CallHistory; theme: ReturnType<typeof getTheme> }) {
  const [playing, setPlaying] = useState(false);
  const [progress, setProgress] = useState(0);
  const intervalRef = useRef<number | null>(null);

  useEffect(() => {
    setPlaying(false);
    setProgress(0);
    if (intervalRef.current) window.clearInterval(intervalRef.current);
  }, [call.id]);

  useEffect(() => {
    if (playing) {
      intervalRef.current = window.setInterval(() => {
        setProgress(prev => {
          // Item 26: fix pacing — exact step, clamp to 100
          const step = 100 / Math.max(call.durationSec, 1);
          const next = Math.min(100, prev + step);
          return next;
        });
      }, 1000);
    } else if (intervalRef.current) {
      window.clearInterval(intervalRef.current);
    }
    return () => { if (intervalRef.current) window.clearInterval(intervalRef.current); };
  }, [playing, call.durationSec]);

  // Item 49: stop playing when progress hits 100 — outside updater
  useEffect(() => {
    if (progress >= 100 && playing) setPlaying(false);
  }, [progress, playing]);

  return (
    <div className="p-4 border-t" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
      <div className="flex items-center gap-3 mb-3">
        <button
          onClick={() => { if (progress >= 100) setProgress(0); setPlaying(p => !p); }}
          className="w-9 h-9 rounded-full flex items-center justify-center text-white shadow-md hover:scale-105 transition-transform shrink-0 focus-visible:ring-2"
          style={{ backgroundColor: theme.primary }}
          aria-label={playing ? 'Pause replay' : 'Play replay'}
        >
          {playing ? <Pause size={15} /> : <Play size={15} fill="currentColor" className="ml-0.5" />}
        </button>
        <div className="flex-1 flex items-center gap-[2px] h-7 cursor-pointer" onClick={(e) => { const rect = e.currentTarget.getBoundingClientRect(); const pct = ((e.clientX - rect.left) / rect.width) * 100; setProgress(Math.max(0, Math.min(100, pct))); }}>
          {Array.from({ length: 28 }, (_, i) => {
            const heights = [10, 18, 24, 14, 26, 12, 20, 16, 28, 12, 22, 9, 18, 25, 11, 19, 14, 23, 10, 16, 21, 13, 27, 15, 20, 11, 17, 24];
            const pct = ((i + 1) / 28) * 100;
            const played = pct <= progress;
            return <span key={i} className="flex-1 rounded-sm transition-colors" style={{ height: `${heights[i % heights.length]}px`, backgroundColor: played ? theme.primary : theme.rule }} />;
          })}
        </div>
        <span className="text-xs font-mono shrink-0" style={{ color: theme.inkMuted }}>
          {formatTime(Math.round((progress / 100) * call.durationSec))} / {formatTime(call.durationSec)}
        </span>
      </div>
      <div className="flex justify-between items-center text-xs" style={{ color: theme.inkMuted }}>
        <div className="flex gap-5">
          <span className="uppercase tracking-wide font-bold">Bot: <span className="ml-1 normal-case" style={{ color: theme.primary }}>{call.bot}</span></span>
          <span className="uppercase tracking-wide font-bold">Avg Turn: <span className={`font-mono ml-1 ${call.avgLatencyMs <= 300 ? 'text-green-600 dark:text-green-400' : 'text-yellow-600 dark:text-yellow-400'}`}>{call.avgLatencyMs}ms</span></span>
        </div>
        {/* Item 39: data residency cue */}
        <span className="text-[10px]" style={{ color: theme.inkMuted }}>Recording · S3 ap-south-1, 30-day retention</span>
      </div>
    </div>
  );
}

// ============================================================================
// Screen 2 — Configuration Panel (PRD Section 9, Screen 2 + Section 6)
// Items 1, 5, 7, 30, 31
// ============================================================================
interface ConfigViewProps {
  bot?: Bot;
  existingConfig?: BotConfig;
  isNew: boolean;
  onSave: (config: BotConfig) => void;
  onCancel: () => void;
  theme: ReturnType<typeof getTheme>;
}

function ConfigView({ bot, existingConfig, onSave, onCancel, theme }: ConfigViewProps) {
  // Seed from existing config > bot defaults > empty
  const seed = existingConfig || (bot ? DEFAULT_CONFIGS[bot.useCase] : undefined);

  const [name, setName] = useState(seed?.name || '');
  const [useCase, setUseCase] = useState(seed?.useCase || 'Credit Card Cross-Sell');
  const [language, setLanguage] = useState(seed?.language || 'Hinglish');
  const [persona, setPersona] = useState(seed?.persona || 'Kore — Female, Firm & Professional');
  const [speed, setSpeed] = useState(seed?.speed || 1.0);
  const [ambientVolume] = useState(seed?.ambientVolume || 20);
  const [ambientProfile, setAmbientProfile] = useState<AmbientProfile>(seed?.ambientProfile || 'callfloor'); // Item 7
  const [backchannels] = useState(seed?.backchannels || ['hmm', 'haan', 'okay', 'achha', 'theek hai', 'yeah']);
  const [ragEnabled, setRagEnabled] = useState(seed?.ragEnabled ?? true);
  const [ragIndex, setRagIndex] = useState(seed?.ragIndex || 'hdfc_regalia_benefits_v2');
  const [greeting, setGreeting] = useState(seed?.greeting || '');
  const [tone] = useState<Tone>(seed?.tone || 'cheerful');

  const handleSave = () => {
    onSave({ name, useCase, language, persona, speed, ambientVolume, ambientProfile, backchannels, ragEnabled, ragIndex, greeting, tone });
  };

  const [disclosureEnabled, setDisclosureEnabled] = useState(true);

  const escalationRules = [
    { condition: 'intent = HIGH_INTEREST', target: 'Sales specialist queue', badge: 'Real-time', badgeColor: theme.primary },
    { condition: 'sentiment < −0.6', target: 'Senior agent queue', badge: 'Real-time', badgeColor: theme.primary },
    { condition: 'customer requests human', target: 'Any available agent', badge: 'Real-time', badgeColor: theme.primary },
    { condition: 'crm.dispute_flag = true', target: 'Do not call · suppress', badge: 'Compliance', badgeColor: theme.destructive },
  ];

  const crmMappings = [
    { variable: '{customer_name}', field: 'crm.profile.full_name', mapped: true },
    { variable: '{product_name}', field: 'crm.offer.product', mapped: true },
    { variable: '{offer_amount}', field: 'crm.offer.limit_inr', mapped: true },
    { variable: '{annual_fee}', field: '—', mapped: false },
  ];

  return (
    <div className="animate-in" style={{ fontFamily: '"DM Sans", sans-serif' }}>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-[22px] font-semibold tracking-tight" style={{ color: theme.ink }}>
            Configure bot
          </h1>
          <p className="text-[13px] mt-0.5" style={{ color: theme.faint }}>
            {bot?.name || name || 'HDFC Credit Card'} · {useCase || 'Cross-sell template'}
          </p>
        </div>
        <span className="px-3 py-1 rounded-md text-[12px] font-medium" style={{ backgroundColor: theme.primaryLight, color: theme.primary }}>Draft — unsaved changes</span>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {/* Left column */}
        <div className="flex flex-col gap-4">
          {/* Bot name & persona */}
          <div className="rounded-lg border overflow-hidden" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
            <div className="p-4 border-b" style={{ borderColor: theme.ruleSubtle }}>
              <div className="text-[14px] font-semibold" style={{ color: theme.ink }}>Bot name & persona</div>
              <div className="text-[12px]" style={{ color: theme.faint }}>Identity used at call start, per RBI disclosure rules</div>
            </div>
            <div className="p-4 space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-[12px] font-medium mb-1.5" style={{ color: theme.ink2 }}>Display name</label>
                  <input type="text" className="w-full border rounded-md px-3 py-2 text-[13px] outline-none focus:ring-2" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard, color: theme.ink, fontFamily: '"DM Sans", sans-serif' }} value={name} onChange={e => setName(e.target.value)} />
                </div>
                <div>
                  <label className="block text-[12px] font-medium mb-1.5" style={{ color: theme.ink2 }}>Voice</label>
                  <div className="flex border rounded-md overflow-hidden" style={{ borderColor: theme.rule }}>
                    {['Female', 'Male'].map(g => (
                      <button key={g} onClick={() => setPersona(g === 'Female' ? 'Kore — Female, Firm & Professional' : 'Charon — Male, Informative')} className="flex-1 py-2 text-[12.5px] font-medium" style={{ backgroundColor: persona.includes(g) ? theme.primary : 'transparent', color: persona.includes(g) ? '#fff' : theme.inkMuted }}>
                        {g}
                      </button>
                    ))}
                  </div>
                </div>
              </div>
              <div>
                <label className="block text-[12px] font-medium mb-1.5" style={{ color: theme.ink2 }}>Speaking speed</label>
                <div className="flex items-center gap-3">
                  <span className="text-[11.5px]" style={{ color: theme.faint }}>Slower</span>
                  <input type="range" min={0.75} max={1.25} step={0.05} value={speed} onChange={e => setSpeed(parseFloat(e.target.value))} className="flex-1 accent-[#053BEE]" />
                  <span className="text-[11.5px]" style={{ color: theme.faint }}>Faster</span>
                  <span className="text-[12px] font-mono" style={{ color: theme.ink2 }}>{speed.toFixed(2)}×</span>
                </div>
              </div>
              <div>
                <label className="block text-[12px] font-medium mb-1.5" style={{ color: theme.ink2 }}>Greeting script template</label>
                <textarea className="w-full border rounded-md px-3 py-2 text-[13px] outline-none focus:ring-2 min-h-[80px] resize-y" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard, color: theme.ink, fontFamily: '"DM Sans", sans-serif' }} value={greeting} onChange={e => setGreeting(e.target.value)} placeholder="Namaste {customer_name} ji, main {bank_name} ki taraf se KOEL bol rahi hoon..." />
                <p className="text-[11px] mt-1" style={{ color: theme.faint }}>Variables in braces resolve from CRM at session start.</p>
              </div>
            </div>
          </div>

          {/* Use case mapping */}
          <div className="rounded-lg border overflow-hidden" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
            <div className="p-4 border-b" style={{ borderColor: theme.ruleSubtle }}>
              <div className="text-[14px] font-semibold" style={{ color: theme.ink }}>Use case mapping</div>
              <div className="text-[12px]" style={{ color: theme.faint }}>Template + CRM field bindings for script variables</div>
            </div>
            <div className="p-4 space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-[12px] font-medium mb-1.5" style={{ color: theme.ink2 }}>Use case template</label>
                  <select className="w-full border rounded-md px-3 py-2 text-[13px] outline-none" style={{ borderColor: theme.rule, color: theme.ink, fontFamily: '"DM Sans", sans-serif' }} value={useCase} onChange={e => setUseCase(e.target.value)}>
                    <option>Credit Card Cross-Sell</option>
                    <option>Collections — Pre-Due</option>
                    <option>Personal Loan Sell</option>
                    <option>Life Insurance Sell</option>
                    <option>Custom</option>
                  </select>
                </div>
                <div>
                  <label className="block text-[12px] font-medium mb-1.5" style={{ color: theme.ink2 }}>Languages</label>
                  <select className="w-full border rounded-md px-3 py-2 text-[13px] outline-none" style={{ borderColor: theme.rule, color: theme.ink, fontFamily: '"DM Sans", sans-serif' }} value={language} onChange={e => setLanguage(e.target.value)}>
                    <option>English · Hindi · Hinglish</option>
                    <option>English only</option>
                    <option>Hindi only</option>
                  </select>
                </div>
              </div>
              <div className="rounded-md border overflow-hidden" style={{ borderColor: theme.ruleSubtle }}>
                <table className="w-full text-[12.5px]">
                  <thead>
                    <tr style={{ backgroundColor: theme.inset }}>
                      <th className="text-left px-3 py-2 font-medium" style={{ color: theme.faint }}>Script variable</th>
                      <th className="text-left px-3 py-2 font-medium" style={{ color: theme.faint }}>CRM field</th>
                      <th className="text-right px-3 py-2 font-medium" style={{ color: theme.faint }}></th>
                    </tr>
                  </thead>
                  <tbody>
                    {crmMappings.map((row, i) => (
                      <tr key={i} className="border-t" style={{ borderColor: theme.ruleSubtle }}>
                        <td className="px-3 py-2 font-mono" style={{ color: theme.ink }}>{row.variable}</td>
                        <td className="px-3 py-2 font-mono" style={{ color: theme.ink }}>{row.field}</td>
                        <td className="px-3 py-2 text-right">
                          <span className="px-2 py-0.5 rounded text-[11px] font-medium" style={{ backgroundColor: row.mapped ? theme.greenLight : theme.warningLight, color: row.mapped ? theme.green : theme.warning }}>
                            {row.mapped ? 'Mapped' : 'Unmapped'}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </div>

        {/* Right column */}
        <div className="flex flex-col gap-4">
          {/* Knowledge & retrieval */}
          <div className="rounded-lg border overflow-hidden" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
            <div className="p-4 border-b" style={{ borderColor: theme.ruleSubtle }}>
              <div className="text-[14px] font-semibold" style={{ color: theme.ink }}>Knowledge & retrieval</div>
              <div className="text-[12px]" style={{ color: theme.faint }}>RAG context retrieved per utterance from pgvector</div>
            </div>
            <div className="p-4 space-y-4">
              <ToggleRow title="Retrieval-augmented generation" sub="Ground responses in product knowledge and objection scripts — no hallucinated product details." checked={ragEnabled} onChange={setRagEnabled} theme={theme} />
              {ragEnabled && (
                <div>
                  <label className="block text-[12px] font-medium mb-1.5" style={{ color: theme.ink2 }}>Knowledge base index</label>
                  <select className="w-full border rounded-md px-3 py-2 text-[13px] outline-none" style={{ borderColor: theme.rule, color: theme.ink, fontFamily: '"DM Sans", sans-serif' }} value={ragIndex} onChange={e => setRagIndex(e.target.value)}>
                    <option>kb_hdfc_regalia_v3</option>
                    <option>kb_hdfc_millennia_v1</option>
                    <option>kb_collections_core</option>
                  </select>
                  <p className="text-[11px] mt-1" style={{ color: theme.faint }}>412 chunks · embeddings refreshed 9 Jun 2026</p>
                </div>
              )}
              <ToggleRow title="Regulatory disclosure block" sub="Non-skippable disclosure script read before any binding step." checked={disclosureEnabled} onChange={setDisclosureEnabled} theme={theme} />
              <ToggleRow title="Ambient call-floor audio" sub="Low-level background chatter mixed under the bot's voice — plays only while the bot speaks, never in silence." checked={ambientProfile !== 'silent'} onChange={(v) => setAmbientProfile(v ? 'callfloor' : 'silent')} theme={theme} />
            </div>
          </div>

          {/* Escalation rules */}
          <div className="rounded-lg border overflow-hidden" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
            <div className="p-4 border-b flex items-center justify-between" style={{ borderColor: theme.ruleSubtle }}>
              <div>
                <div className="text-[14px] font-semibold" style={{ color: theme.ink }}>Escalation rules</div>
                <div className="text-[12px]" style={{ color: theme.faint }}>Hand off to human agent queue with full context</div>
              </div>
              <button className="text-[12.5px] font-medium" style={{ color: theme.primary }}>+ Add rule</button>
            </div>
            <div className="p-4 space-y-2.5">
              {escalationRules.map((rule, i) => (
                <div key={i} className="flex items-center justify-between py-2.5 px-3 rounded-md border" style={{ borderColor: theme.ruleSubtle, backgroundColor: theme.inset }}>
                  <div className="flex items-center gap-2 text-[12.5px]">
                    <code className="px-1.5 py-0.5 rounded text-[11px] font-mono font-medium" style={{ backgroundColor: theme.primaryLight, color: theme.primary }}>{rule.condition}</code>
                    <span style={{ color: theme.faint }}>→</span>
                    <span style={{ color: theme.ink }}>{rule.target}</span>
                  </div>
                  <span className="px-2 py-0.5 rounded text-[10.5px] font-medium" style={{ backgroundColor: rule.badge === 'Compliance' ? theme.destructiveLight : theme.primaryLight, color: rule.badgeColor }}>{rule.badge}</span>
                </div>
              ))}
            </div>
          </div>

          {/* Success criteria */}
          <div className="rounded-lg border overflow-hidden" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
            <div className="p-4 border-b" style={{ borderColor: theme.ruleSubtle }}>
              <div className="text-[14px] font-semibold" style={{ color: theme.ink }}>Success criteria</div>
              <div className="text-[12px]" style={{ color: theme.faint }}>What this bot is measured on</div>
            </div>
            <div className="p-4">
              <KV k="Primary outcome" v="Warm lead escalated" theme={theme} />
              <KV k="Demo benchmark" v=">12 leads / 100 calls" theme={theme} />
              <KV k="Hard rejection handling" v="Log + suppress 30 days" theme={theme} />
            </div>
          </div>
        </div>
      </div>

      {/* Save bar */}
      <div className="mt-4 flex items-center justify-between py-3 px-4 rounded-lg border" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
        <span className="text-[12px] flex items-center gap-1.5" style={{ color: theme.faint }}><Lock size={12} /> Changes apply on next session start. Active calls are not interrupted.</span>
        <div className="flex gap-3">
          <button onClick={onCancel} className="px-4 py-2 rounded-md border text-[13px] font-medium" style={{ borderColor: theme.rule, color: theme.inkMuted }}>Discard</button>
          <button onClick={handleSave} className="px-5 py-2 rounded-md text-[13px] font-bold text-white" style={{ backgroundColor: theme.primary }}>Save configuration</button>
        </div>
      </div>
    </div>
  );
}

function ToggleRow({ title, sub, checked, onChange, theme }: { title: string; sub: string; checked: boolean; onChange: (v: boolean) => void; theme: ReturnType<typeof getTheme> }) {
  return (
    <div className="flex items-center justify-between py-2">
      <div className="pr-4">
        <div className="text-[13px] font-medium" style={{ color: theme.ink }}>{title}</div>
        <div className="text-[11.5px]" style={{ color: theme.faint }}>{sub}</div>
      </div>
      <button onClick={() => onChange(!checked)} className="w-9 h-5 rounded-full relative transition-colors shrink-0" style={{ backgroundColor: checked ? theme.primary : theme.rule }}>
        <span className="absolute top-0.5 w-4 h-4 rounded-full bg-white shadow transition-all" style={{ left: checked ? '18px' : '2px' }} />
      </button>
    </div>
  );
}

// ============================================================================
// Screen 5 — Bot Builder (5-step wizard)
// ============================================================================
type BuilderStep = 1 | 2 | 3 | 4 | 5;
const BUILDER_STEPS: { num: BuilderStep; title: string; sub: string }[] = [
  { num: 1, title: 'Use case', sub: 'Template & languages' },
  { num: 2, title: 'Persona & voice', sub: 'Name, gender, speed' },
  { num: 3, title: 'Conversation flow', sub: 'Script nodes & branches' },
  { num: 4, title: 'Knowledge & CRM', sub: 'RAG index · field map' },
  { num: 5, title: 'Review & deploy', sub: 'Preflight checks' },
];

const FLOW_NODES = [
  { type: 'greet', label: 'Greeting', title: 'Personalised opener', detail: 'CRM: name, bank, product, offer' },
  { type: 'listen', label: 'Listen', title: 'Capture & classify intent', detail: 'STT → Groq intent tag' },
];

const FLOW_BRANCHES = [
  { type: 'rag', label: 'RAG response', title: 'Pitch benefits', detail: 'on intent = INTERESTED' },
  { type: 'rag', label: 'RAG response', title: 'Handle objection', detail: 'on intent = OBJECTION' },
  { type: 'escalate', label: 'Escalate', title: 'Hand to specialist', detail: 'on intent = HIGH_INTEREST' },
  { type: 'end', label: 'End', title: 'Log & close', detail: 'on intent = REJECTION · suppress 30d' },
];

function BotBuilderView({ onDeploy, onCancel, theme }: { onDeploy: (config: BotConfig) => void; onCancel: () => void; theme: ReturnType<typeof getTheme> }) {
  const [step, setStep] = useState<BuilderStep>(1);
  const [botName, setBotName] = useState('');
  const [useCase, setUseCase] = useState('Credit Card Cross-Sell');
  const [languages, setLanguages] = useState<string[]>(['English', 'Hindi', 'Hinglish']);
  const [voiceGender, setVoiceGender] = useState('Female');
  const [voiceSpeed, setVoiceSpeed] = useState(1.05);
  const [disclosure, setDisclosure] = useState(true);
  const [ambientEnabled, setAmbientEnabled] = useState(true);
  const [ragIndex, setRagIndex] = useState('kb_hdfc_regalia_v3');
  const [selectedNode, setSelectedNode] = useState<string | null>('Handle objection');
  const [nodeScript, setNodeScript] = useState('Bilkul samajh sakti hoon. Yeh card {benefit_1} aur {benefit_2} deta hai — aapke existing cards se alag benefit. {fee_clause}. Interested hain?');

  const nodeColor = (type: string) => {
    switch (type) {
      case 'greet': return theme.primary;
      case 'listen': return theme.faint;
      case 'rag': return theme.greenMid;
      case 'escalate': return theme.accent;
      case 'end': return theme.destructive;
      default: return theme.faint;
    }
  };

  const preflight = [
    { label: 'All script variables mapped to CRM', ok: true },
    { label: 'Knowledge base index reachable', ok: true },
    { label: 'Compliance disclosure block present', ok: disclosure },
    { label: 'Call-hours window not yet set (RBI)', ok: false },
  ];

  const handleDeploy = () => {
    onDeploy({
      name: botName || 'KOEL — New Bot',
      useCase,
      language: languages.join(', '),
      persona: `KOEL · ${voiceGender} · ${voiceSpeed}×`,
      speed: voiceSpeed,
      ambientVolume: 20,
      ambientProfile: ambientEnabled ? 'callfloor' : 'silent',
      backchannels: ['hmm', 'haan', 'okay'],
      ragEnabled: true,
      ragIndex,
      greeting: '',
      tone: 'cheerful',
    });
  };

  return (
    <div className="animate-in" style={{ fontFamily: '"DM Sans", sans-serif' }}>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-[22px] font-semibold tracking-tight" style={{ color: theme.ink }}>
            {step === 5 ? 'Review & deploy' : 'Build a new bot'}
          </h1>
          <p className="text-[13px] mt-0.5" style={{ color: theme.faint }}>
            Step {step} of 5 · {BUILDER_STEPS[step - 1].title}{step > 1 ? ` · ${useCase}` : ''}
          </p>
        </div>
        <button onClick={onCancel} className="text-[13px] font-medium px-3 py-1.5 rounded-md hover:opacity-80" style={{ color: theme.inkMuted }}>Cancel</button>
      </div>

      <div className="grid gap-6" style={{ gridTemplateColumns: '200px 1fr' }}>
        {/* Stepper */}
        <div className="flex flex-col gap-0">
          {BUILDER_STEPS.map((s, i) => (
            <React.Fragment key={s.num}>
              <button
                onClick={() => s.num < step && setStep(s.num)}
                className="flex items-start gap-3 text-left py-2 px-2 rounded-md"
                style={{ cursor: s.num < step ? 'pointer' : s.num === step ? 'default' : 'default', opacity: s.num > step ? 0.5 : 1 }}
              >
                <span
                  className="w-[22px] h-[22px] rounded-full flex items-center justify-center text-[11px] font-bold shrink-0 mt-0.5"
                  style={{
                    backgroundColor: s.num < step ? theme.green : s.num === step ? theme.primary : theme.ruleSubtle,
                    color: s.num <= step ? '#fff' : theme.faint,
                  }}
                >
                  {s.num < step ? '✓' : s.num}
                </span>
                <span>
                  <span className="block text-[13px] font-medium" style={{ color: s.num === step ? theme.ink : theme.inkMuted }}>{s.title}</span>
                  {s.num <= step && <span className="block text-[11.5px]" style={{ color: theme.faint }}>{s.sub}</span>}
                </span>
              </button>
              {i < BUILDER_STEPS.length - 1 && (
                <div className="w-px h-3 ml-[12px]" style={{ backgroundColor: s.num < step ? theme.green : theme.ruleSubtle }} />
              )}
            </React.Fragment>
          ))}
        </div>

        {/* Main panel */}
        <div className="rounded-lg border overflow-hidden" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
          <div className="p-5 border-b" style={{ borderColor: theme.ruleSubtle }}>
            <h2 className="text-[15px] font-semibold" style={{ color: theme.ink }}>{BUILDER_STEPS[step - 1].title}</h2>
            <p className="text-[12.5px] mt-1" style={{ color: theme.faint }}>
              {step === 1 && 'Select the use case template and supported languages.'}
              {step === 2 && 'Configure the bot persona, voice, and speech settings.'}
              {step === 3 && 'Define how KOEL moves through the call. Each node is a script step; branches are driven by intent classification.'}
              {step === 4 && 'Connect the RAG knowledge base and map CRM fields to script variables.'}
              {step === 5 && 'Review the configuration below. Once deployed, the bot appears on the dashboard.'}
            </p>
          </div>

          <div className="p-5">
            {/* Step 1: Use case */}
            {step === 1 && (
              <div className="space-y-5">
                <div>
                  <label className="block text-[12px] font-medium mb-1.5" style={{ color: theme.ink2 }}>Bot display name</label>
                  <input value={botName} onChange={e => setBotName(e.target.value)} placeholder="KOEL — HDFC Regalia" className="w-full border rounded-md px-3 py-2 text-[13px] outline-none focus:ring-2" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard, color: theme.ink, fontFamily: '"DM Sans", sans-serif' }} />
                </div>
                <div>
                  <label className="block text-[12px] font-medium mb-1.5" style={{ color: theme.ink2 }}>Use case template</label>
                  <select value={useCase} onChange={e => setUseCase(e.target.value)} className="w-full border rounded-md px-3 py-2 text-[13px] outline-none" style={{ borderColor: theme.rule, color: theme.ink, fontFamily: '"DM Sans", sans-serif' }}>
                    <option>Credit Card Cross-Sell</option>
                    <option>Collections Pre-Due</option>
                    <option>Personal Loan Sell</option>
                    <option>Life Insurance Sell</option>
                  </select>
                </div>
                <div>
                  <label className="block text-[12px] font-medium mb-1.5" style={{ color: theme.ink2 }}>Languages</label>
                  <div className="flex flex-wrap gap-2">
                    {['English', 'Hindi', 'Hinglish', 'Tamil', 'Telugu', 'Marathi'].map(lang => (
                      <button key={lang} onClick={() => setLanguages(prev => prev.includes(lang) ? prev.filter(l => l !== lang) : [...prev, lang])} className="px-3 py-1.5 rounded-md text-[12.5px] font-medium border" style={{ borderColor: languages.includes(lang) ? theme.primary : theme.rule, backgroundColor: languages.includes(lang) ? theme.primaryLight : 'transparent', color: languages.includes(lang) ? theme.primary : theme.inkMuted }}>
                        {lang}
                      </button>
                    ))}
                  </div>
                </div>
              </div>
            )}

            {/* Step 2: Persona & voice */}
            {step === 2 && (
              <div className="space-y-5">
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label className="block text-[12px] font-medium mb-1.5" style={{ color: theme.ink2 }}>Voice gender</label>
                    <select value={voiceGender} onChange={e => setVoiceGender(e.target.value)} className="w-full border rounded-md px-3 py-2 text-[13px]" style={{ borderColor: theme.rule, color: theme.ink, fontFamily: '"DM Sans", sans-serif' }}>
                      <option>Female</option>
                      <option>Male</option>
                    </select>
                  </div>
                  <div>
                    <label className="block text-[12px] font-medium mb-1.5" style={{ color: theme.ink2 }}>Speed</label>
                    <div className="flex items-center gap-3">
                      <input type="range" min={0.8} max={1.3} step={0.05} value={voiceSpeed} onChange={e => setVoiceSpeed(parseFloat(e.target.value))} className="flex-1" />
                      <span className="text-[13px] font-mono w-12 text-right" style={{ color: theme.ink }}>{voiceSpeed.toFixed(2)}×</span>
                    </div>
                  </div>
                </div>
                <div className="flex items-center justify-between py-3 px-4 rounded-md border" style={{ borderColor: theme.ruleSubtle, backgroundColor: theme.inset }}>
                  <div>
                    <span className="text-[13px] font-medium" style={{ color: theme.ink }}>Compliance disclosure block</span>
                    <p className="text-[11.5px]" style={{ color: theme.faint }}>RBI-mandated disclosure played at start of call</p>
                  </div>
                  <button onClick={() => setDisclosure(!disclosure)} className="w-9 h-5 rounded-full relative transition-colors" style={{ backgroundColor: disclosure ? theme.primary : theme.rule }}>
                    <span className="absolute top-0.5 w-4 h-4 rounded-full bg-white shadow transition-all" style={{ left: disclosure ? '18px' : '2px' }} />
                  </button>
                </div>
                <div className="flex items-center justify-between py-3 px-4 rounded-md border" style={{ borderColor: theme.ruleSubtle, backgroundColor: theme.inset }}>
                  <div>
                    <span className="text-[13px] font-medium" style={{ color: theme.ink }}>Ambient call-floor audio</span>
                    <p className="text-[11.5px]" style={{ color: theme.faint }}>Background chatter under the bot's voice — only while it speaks, never in silence</p>
                  </div>
                  <button onClick={() => setAmbientEnabled(!ambientEnabled)} className="w-9 h-5 rounded-full relative transition-colors" style={{ backgroundColor: ambientEnabled ? theme.primary : theme.rule }}>
                    <span className="absolute top-0.5 w-4 h-4 rounded-full bg-white shadow transition-all" style={{ left: ambientEnabled ? '18px' : '2px' }} />
                  </button>
                </div>
              </div>
            )}

            {/* Step 3: Conversation flow */}
            {step === 3 && (
              <div>
                <div className="flex items-center justify-between mb-4">
                  <div className="flex gap-4 text-[11.5px]" style={{ color: theme.faint }}>
                    {[{ c: theme.primary, l: 'Greeting' }, { c: theme.faint, l: 'Listen' }, { c: theme.greenMid, l: 'RAG response' }, { c: theme.accent, l: 'Escalate' }, { c: theme.destructive, l: 'End' }].map(lg => (
                      <span key={lg.l} className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full" style={{ backgroundColor: lg.c }} />{lg.l}</span>
                    ))}
                  </div>
                  <button className="text-[12.5px] font-medium px-3 py-1 border rounded-md" style={{ borderColor: theme.rule, color: theme.inkMuted }}>Test flow</button>
                </div>

                <div className="p-4 rounded-lg" style={{ backgroundColor: theme.inset }}>
                  {/* Linear nodes */}
                  {FLOW_NODES.map((n, i) => (
                    <React.Fragment key={i}>
                      <div className="p-3 rounded-md border cursor-pointer" style={{ borderColor: theme.ruleSubtle, backgroundColor: theme.surfaceCard }} onClick={() => setSelectedNode(n.title)}>
                        <span className="inline-flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-wider mb-1" style={{ color: nodeColor(n.type) }}>
                          <span className="w-[5px] h-[5px] rounded-full" style={{ backgroundColor: nodeColor(n.type) }} />{n.label}
                        </span>
                        <div className="text-[13px] font-medium" style={{ color: theme.ink }}>{n.title}</div>
                        <div className="text-[11.5px]" style={{ color: theme.faint }}>{n.detail}</div>
                      </div>
                      <div className="w-px h-4 mx-auto" style={{ backgroundColor: theme.rule }} />
                    </React.Fragment>
                  ))}

                  {/* Branches */}
                  <div className="grid grid-cols-4 gap-3 mt-1">
                    {FLOW_BRANCHES.map((b, i) => (
                      <div key={i} className="p-3 rounded-md border cursor-pointer" style={{ borderColor: selectedNode === b.title ? theme.primary : theme.ruleSubtle, backgroundColor: selectedNode === b.title ? theme.primarySubtle : theme.surfaceCard }} onClick={() => { setSelectedNode(b.title); setNodeScript(b.detail); }}>
                        <span className="inline-flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-wider mb-1" style={{ color: nodeColor(b.type) }}>
                          <span className="w-[5px] h-[5px] rounded-full" style={{ backgroundColor: nodeColor(b.type) }} />{b.label}
                        </span>
                        <div className="text-[12.5px] font-medium" style={{ color: theme.ink }}>{b.title}</div>
                        <div className="text-[11px]" style={{ color: theme.faint }}>{b.detail}</div>
                      </div>
                    ))}
                  </div>
                </div>

                {selectedNode && (
                  <div className="flex gap-4 mt-4">
                    <div className="flex-1">
                      <label className="block text-[12px] font-medium mb-1.5" style={{ color: theme.ink2 }}>Selected node — {selectedNode}</label>
                      <textarea value={nodeScript} onChange={e => setNodeScript(e.target.value)} className="w-full border rounded-md px-3 py-2 text-[13px] outline-none min-h-[64px] resize-y" style={{ borderColor: theme.rule, color: theme.ink, fontFamily: '"DM Sans", sans-serif' }} />
                    </div>
                    <div className="w-[200px]">
                      <label className="block text-[12px] font-medium mb-1.5" style={{ color: theme.ink2 }}>Knowledge source</label>
                      <select value={ragIndex} onChange={e => setRagIndex(e.target.value)} className="w-full border rounded-md px-3 py-2 text-[13px]" style={{ borderColor: theme.rule, color: theme.ink, fontFamily: '"DM Sans", sans-serif' }}>
                        <option>kb_hdfc_regalia_v3</option>
                        <option>kb_hdfc_millennia_v1</option>
                        <option>kb_icici_plat_v2</option>
                      </select>
                      <p className="text-[11px] mt-2" style={{ color: theme.faint }}>Retrieved per utterance — no hallucinated product details.</p>
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* Step 4: Knowledge & CRM */}
            {step === 4 && (
              <div className="space-y-5">
                <div>
                  <label className="block text-[12px] font-medium mb-1.5" style={{ color: theme.ink2 }}>RAG knowledge index</label>
                  <input value={ragIndex} onChange={e => setRagIndex(e.target.value)} className="w-full border rounded-md px-3 py-2 text-[13px] font-mono" style={{ borderColor: theme.rule, color: theme.ink }} />
                </div>
                <div>
                  <label className="block text-[12px] font-medium mb-3" style={{ color: theme.ink2 }}>CRM variable mapping</label>
                  <div className="rounded-md border overflow-hidden" style={{ borderColor: theme.rule }}>
                    <table className="w-full text-[12.5px]">
                      <thead>
                        <tr style={{ backgroundColor: theme.inset }}>
                          <th className="text-left px-3 py-2 font-medium" style={{ color: theme.faint }}>Script variable</th>
                          <th className="text-left px-3 py-2 font-medium" style={{ color: theme.faint }}>CRM field</th>
                          <th className="text-left px-3 py-2 font-medium" style={{ color: theme.faint }}>Status</th>
                        </tr>
                      </thead>
                      <tbody>
                        {[
                          { variable: '{customer_name}', field: 'crm.full_name', mapped: true },
                          { variable: '{product_name}', field: 'crm.offer.product', mapped: true },
                          { variable: '{benefit_1}', field: 'crm.offer.benefit_primary', mapped: true },
                          { variable: '{benefit_2}', field: 'crm.offer.benefit_secondary', mapped: true },
                          { variable: '{fee_clause}', field: 'crm.offer.fee_waiver', mapped: true },
                          { variable: '{pre_approved_limit}', field: '—', mapped: false },
                        ].map((row, i) => (
                          <tr key={i} className="border-t" style={{ borderColor: theme.ruleSubtle }}>
                            <td className="px-3 py-2 font-mono" style={{ color: theme.primary }}>{row.variable}</td>
                            <td className="px-3 py-2 font-mono" style={{ color: theme.ink }}>{row.field}</td>
                            <td className="px-3 py-2">
                              <span className="px-2 py-0.5 rounded text-[11px] font-medium" style={{ backgroundColor: row.mapped ? theme.greenLight : theme.warningLight, color: row.mapped ? theme.green : theme.warning }}>
                                {row.mapped ? 'Mapped' : 'Unmapped'}
                              </span>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              </div>
            )}

            {/* Step 5: Review */}
            {step === 5 && (
              <div className="grid grid-cols-2 gap-4">
                <ReviewBlock title="Identity" theme={theme}>
                  <KV k="Display name" v={botName || 'KOEL — New Bot'} theme={theme} />
                  <KV k="Use case" v={useCase} theme={theme} />
                  <KV k="Direction" v="Outgoing" theme={theme} />
                  <KV k="Languages" v={languages.join(' · ')} theme={theme} />
                </ReviewBlock>
                <ReviewBlock title="Voice" theme={theme}>
                  <KV k="Voice" v={voiceGender} theme={theme} />
                  <KV k="Speed" v={`${voiceSpeed.toFixed(2)}×`} theme={theme} />
                  <KV k="Greeting" v="Personalised template" theme={theme} />
                  <KV k="Disclosure block" v={disclosure ? 'Enabled' : 'Disabled'} theme={theme} />
                </ReviewBlock>
                <ReviewBlock title="Flow & knowledge" theme={theme}>
                  <KV k="Script nodes" v={`${FLOW_NODES.length + FLOW_BRANCHES.length}`} theme={theme} />
                  <KV k="Branches" v={`${FLOW_BRANCHES.length}`} theme={theme} />
                  <KV k="RAG index" v={ragIndex} theme={theme} mono />
                  <KV k="Escalation rules" v="4" theme={theme} />
                </ReviewBlock>
                <ReviewBlock title="Preflight checks" theme={theme}>
                  {preflight.map((pf, i) => (
                    <div key={i} className="flex items-start gap-2 text-[12.5px] py-1">
                      {pf.ok ? (
                        <CheckCircle2 size={14} style={{ color: theme.green, flexShrink: 0, marginTop: 1 }} />
                      ) : (
                        <AlertTriangle size={14} style={{ color: theme.warning, flexShrink: 0, marginTop: 1 }} />
                      )}
                      <span style={{ color: pf.ok ? theme.ink : theme.warning }}>{pf.label}</span>
                    </div>
                  ))}
                </ReviewBlock>
              </div>
            )}
          </div>

          {/* Footer */}
          <div className="flex items-center justify-between p-4 border-t" style={{ borderColor: theme.ruleSubtle }}>
            <button onClick={() => step > 1 ? setStep((step - 1) as BuilderStep) : onCancel()} className="text-[13px] font-medium px-4 py-2 border rounded-md" style={{ borderColor: theme.rule, color: theme.inkMuted }}>
              ← Back
            </button>
            <span className="text-[12px]" style={{ color: theme.faint }}>
              {step === 3 && `${FLOW_BRANCHES.length} branches · ${FLOW_NODES.length + FLOW_BRANCHES.length} nodes · 0 unmapped variables`}
              {step === 5 && `${preflight.filter(p => p.ok).length} of ${preflight.length} checks passed · ${preflight.filter(p => !p.ok).length} warning`}
            </span>
            {step < 5 ? (
              <button onClick={() => setStep((step + 1) as BuilderStep)} className="text-[13px] font-bold px-5 py-2 rounded-md text-white" style={{ backgroundColor: theme.primary }}>
                Continue →
              </button>
            ) : (
              <button onClick={handleDeploy} className="text-[13px] font-bold px-5 py-2 rounded-md text-white" style={{ backgroundColor: theme.primary }}>
                Deploy bot
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function ReviewBlock({ title, children, theme }: { title: string; children: React.ReactNode; theme: ReturnType<typeof getTheme> }) {
  return (
    <div className="rounded-md border p-4" style={{ borderColor: theme.ruleSubtle }}>
      <div className="text-[12px] font-bold uppercase tracking-wider mb-3" style={{ color: theme.faint }}>{title}</div>
      <div className="space-y-1">{children}</div>
    </div>
  );
}

function KV({ k, v, theme, mono }: { k: string; v: string; theme: ReturnType<typeof getTheme>; mono?: boolean }) {
  return (
    <div className="flex justify-between text-[12.5px] py-1">
      <span style={{ color: theme.faint }}>{k}</span>
      <span className={mono ? 'font-mono' : ''} style={{ color: theme.ink, fontWeight: 500 }}>{v}</span>
    </div>
  );
}

// ============================================================================
// Screen 6 — Knowledge Base
// ============================================================================
function KnowledgeBaseView({ theme, token, onToast }: { theme: ReturnType<typeof getTheme>; token: string | null; onToast: (m: string) => void }) {
  const [search, setSearch] = useState('');
  const [indexes, setIndexes] = useState<KnowledgeIndex[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ rag_index: '', use_case: 'credit_card', title: '', content: '' });
  const [saving, setSaving] = useState(false);
  const [showUpload, setShowUpload] = useState(false);
  const [uploadMeta, setUploadMeta] = useState({ rag_index: '', use_case: 'credit_card' });
  const [uploading, setUploading] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    if (!token) { setLoading(false); return; }
    setLoading(true);
    try {
      setIndexes(await getKnowledgeIndexes(token));
      setError(null);
    } catch (e) {
      setError(e instanceof KoelApiError ? e.message : 'Could not load knowledge indexes.');
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => { void refresh(); }, [refresh]);

  const submit = async () => {
    if (!token || !form.rag_index.trim() || !form.title.trim() || !form.content.trim()) return;
    setSaving(true);
    try {
      await createKnowledgeChunk(token, { ...form, chunk: form.content.length > 1200 });
      onToast(`Knowledge added to “${form.rag_index}” and embedded for retrieval.`);
      setForm({ rag_index: '', use_case: 'credit_card', title: '', content: '' });
      setShowForm(false);
      await refresh();
    } catch (e) {
      onToast(e instanceof KoelApiError ? e.message : 'Failed to add knowledge.');
    } finally {
      setSaving(false);
    }
  };

  const doUpload = async () => {
    const file = fileRef.current?.files?.[0];
    if (!token || !uploadMeta.rag_index.trim() || !file) return;
    setUploading(true);
    try {
      const chunks = await uploadKnowledgeFile(token, {
        file, ragIndex: uploadMeta.rag_index.trim(), useCase: uploadMeta.use_case,
      });
      onToast(`Ingested “${file.name}” → ${chunks.length} chunk${chunks.length === 1 ? '' : 's'} embedded into “${uploadMeta.rag_index}”.`);
      if (fileRef.current) fileRef.current.value = '';
      setUploadMeta({ rag_index: '', use_case: 'credit_card' });
      setShowUpload(false);
      await refresh();
    } catch (e) {
      onToast(e instanceof KoelApiError ? e.message : 'File upload failed.');
    } finally {
      setUploading(false);
    }
  };

  const filtered = indexes.filter(kb => kb.rag_index.toLowerCase().includes(search.toLowerCase()));
  const totalChunks = indexes.reduce((s, k) => s + k.chunk_count, 0);

  return (
    <div className="animate-in" style={{ fontFamily: '"DM Sans", sans-serif' }}>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-[22px] font-semibold tracking-tight" style={{ color: theme.ink }}>Knowledge base</h1>
          <p className="text-[13px] mt-0.5" style={{ color: theme.faint }}>{indexes.length} indexes · {totalChunks} chunks</p>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={() => { setShowUpload(v => !v); setShowForm(false); }} className="flex items-center gap-2 text-[13px] font-semibold px-4 py-2 rounded-md border" style={{ borderColor: theme.rule, color: theme.ink }}>
            <BookOpen size={14} /> {showUpload ? 'Close' : 'Upload file'}
          </button>
          <button onClick={() => { setShowForm(v => !v); setShowUpload(false); }} className="flex items-center gap-2 text-[13px] font-bold px-4 py-2 rounded-md text-white" style={{ backgroundColor: theme.primary }}>
            <Plus size={14} /> {showForm ? 'Close' : 'New chunk'}
          </button>
        </div>
      </div>

      {showUpload && (
        <div className="rounded-lg border p-4 mb-4 grid gap-3" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
          <div className="grid grid-cols-2 gap-3">
            <input value={uploadMeta.rag_index} onChange={e => setUploadMeta({ ...uploadMeta, rag_index: e.target.value })} placeholder="RAG index (e.g. cc_hdfc)" className="border rounded-md px-3 py-2 text-[13px] outline-none focus:ring-2" style={{ borderColor: theme.rule, backgroundColor: theme.inset, color: theme.ink }} />
            <select value={uploadMeta.use_case} onChange={e => setUploadMeta({ ...uploadMeta, use_case: e.target.value })} className="border rounded-md px-3 py-2 text-[13px] outline-none focus:ring-2" style={{ borderColor: theme.rule, backgroundColor: theme.inset, color: theme.ink }}>
              {['credit_card', 'collections', 'personal_loan', 'insurance', 'custom'].map(u => <option key={u} value={u}>{u}</option>)}
            </select>
          </div>
          <input ref={fileRef} type="file" accept=".txt,.md,.csv,.pdf,text/plain,application/pdf" className="text-[13px]" style={{ color: theme.ink }} />
          <p className="text-[11px]" style={{ color: theme.faint }}>Text or PDF, up to 2 MB. The file is split into passages, embedded, and stored under the index above.</p>
          <div className="flex justify-end">
            <button onClick={() => void doUpload()} disabled={uploading} className="text-[13px] font-bold px-4 py-2 rounded-md text-white disabled:opacity-50" style={{ backgroundColor: theme.primary }}>
              {uploading ? 'Ingesting…' : 'Upload & embed'}
            </button>
          </div>
        </div>
      )}

      {showForm && (
        <div className="rounded-lg border p-4 mb-4 grid gap-3" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
          <div className="grid grid-cols-2 gap-3">
            <input value={form.rag_index} onChange={e => setForm({ ...form, rag_index: e.target.value })} placeholder="RAG index (e.g. cc_hdfc)" className="border rounded-md px-3 py-2 text-[13px] outline-none focus:ring-2" style={{ borderColor: theme.rule, backgroundColor: theme.inset, color: theme.ink }} />
            <select value={form.use_case} onChange={e => setForm({ ...form, use_case: e.target.value })} className="border rounded-md px-3 py-2 text-[13px] outline-none focus:ring-2" style={{ borderColor: theme.rule, backgroundColor: theme.inset, color: theme.ink }}>
              {['credit_card', 'collections', 'personal_loan', 'insurance', 'custom'].map(u => <option key={u} value={u}>{u}</option>)}
            </select>
          </div>
          <input value={form.title} onChange={e => setForm({ ...form, title: e.target.value })} placeholder="Title (e.g. Objection: annual fee)" className="border rounded-md px-3 py-2 text-[13px] outline-none focus:ring-2" style={{ borderColor: theme.rule, backgroundColor: theme.inset, color: theme.ink }} />
          <textarea value={form.content} onChange={e => setForm({ ...form, content: e.target.value })} placeholder="Knowledge content — this is embedded and used to ground responses." rows={3} className="border rounded-md px-3 py-2 text-[13px] outline-none focus:ring-2 resize-y" style={{ borderColor: theme.rule, backgroundColor: theme.inset, color: theme.ink }} />
          <div className="flex justify-end">
            <button onClick={() => void submit()} disabled={saving} className="text-[13px] font-bold px-4 py-2 rounded-md text-white disabled:opacity-50" style={{ backgroundColor: theme.primary }}>
              {saving ? 'Embedding…' : 'Add & embed'}
            </button>
          </div>
        </div>
      )}

      <div className="mb-4">
        <div className="relative">
          <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2" style={{ color: theme.faint }} />
          <input value={search} onChange={e => setSearch(e.target.value)} placeholder="Search indexes…" className="w-full border rounded-md pl-9 pr-3 py-2 text-[13px] outline-none focus:ring-2" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard, color: theme.ink, fontFamily: '"DM Sans", sans-serif' }} />
        </div>
      </div>

      <div className="rounded-lg border overflow-hidden" style={{ borderColor: theme.rule, backgroundColor: theme.surfaceCard }}>
        <table className="w-full text-[13px]">
          <thead>
            <tr style={{ backgroundColor: theme.inset }}>
              <th className="text-left px-4 py-2.5 font-medium text-[12px]" style={{ color: theme.faint }}>Index ID</th>
              <th className="text-right px-4 py-2.5 font-medium text-[12px]" style={{ color: theme.faint }}>Chunks</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map(kb => (
              <tr key={kb.rag_index} className="border-t" style={{ borderColor: theme.ruleSubtle }}>
                <td className="px-4 py-3 font-mono text-[12px]" style={{ color: theme.primary }}>{kb.rag_index}</td>
                <td className="px-4 py-3 text-right font-mono" style={{ color: theme.ink }}>{kb.chunk_count}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {loading && <div className="p-8 text-center text-[13px]" style={{ color: theme.faint }}>Loading indexes…</div>}
        {!loading && error && <div className="p-8 text-center text-[13px]" style={{ color: theme.warning }}>{error}</div>}
        {!loading && !error && filtered.length === 0 && (
          <div className="p-8 text-center text-[13px]" style={{ color: theme.faint }}>No indexes yet — add a knowledge chunk to create one.</div>
        )}
      </div>
    </div>
  );
}


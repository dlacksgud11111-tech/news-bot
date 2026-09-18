/**
 * news-bot-messages — 1분마다 GitHub Actions 깨우기
 *
 * 무엇을 위한 것인가
 *   봇 DM 에 기사 링크를 넣으면 몇십 초 안에 📝 기사 요약 채널에 카드가 올라오게
 *   합니다. 뉴스 수집은 건드리지 않습니다(그건 wake-news-bot.gs 가 5분마다).
 *
 * 왜 GitHub 예약을 안 쓰는가
 *   2026-09-18 실측: 30분 간격으로 걸어둔 예약이 하루 세 번만 실행됐습니다
 *   (00:07 / 04:08 / 07:18). 그 사이에 보낸 링크는 아무 답이 없었습니다.
 *   구글 트리거는 구글 서버에서 도니까 제 시각에 뜨고, PC 가 꺼져 있어도 됩니다.
 *
 * 이 파일은 '뉴스 클리핑 트리거' 프로젝트에 덧붙이는 것을 전제로 합니다.
 * 기존 이름(NB_*, OWNER, setupTrigger …)과 겹치지 않게 전부 NBM_ 을 붙였습니다.
 * GITHUB_TOKEN 은 그 프로젝트에 이미 있는 것을 같이 씁니다.
 *
 * 설치
 *   1) 이 내용을 프로젝트에 새 파일로 붙여넣고 저장
 *   2) 함수 목록에서 testNewsBotMessages 실행 → 로그에 '성공' 이 뜨는지 확인
 *   3) 함수 목록에서 setupNewsBotMessagesTrigger 실행 → 1분마다 자동
 *
 * 할당량
 *   1분 간격 = 하루 1,440회, 한 번에 1초도 안 걸립니다.
 *   무료 한도(하루 90분, UrlFetch 2만회) 안에 들어갑니다.
 *   기존 5분 트리거(288회)와 합쳐도 넉넉합니다.
 */

const NBM_OWNER = 'dlacksgud11111-tech';
const NBM_REPO = 'news-bot';
const NBM_WORKFLOW = 'messages.yml';

/** 1분마다 트리거가 부르는 함수입니다. */
function wakeNewsBotMessages() {
  dispatchNewsBotMessages();
}

/** 설치 직후 손으로 한 번 실행해 연결을 확인하는 용도입니다. */
function testNewsBotMessages() {
  const code = dispatchNewsBotMessages();
  if (code === 204) {
    Logger.log('성공 — GitHub Actions 탭에 news-bot-messages 실행이 떴는지 확인하세요.');
  } else if (code === 404) {
    Logger.log('실패 404 — 워크플로 파일이 아직 main 에 없거나, 토큰에 news-bot ' +
               '저장소 권한이 없습니다. messages.yml 이 올라갔는지 먼저 보세요.');
  } else {
    Logger.log('실패 — 응답 코드 ' + code);
  }
}

/** 1분 트리거를 겁니다. 이미 걸려 있으면 지우고 새로 겁니다. */
function setupNewsBotMessagesTrigger() {
  removeNewsBotMessagesTrigger();
  ScriptApp.newTrigger('wakeNewsBotMessages').timeBased().everyMinutes(1).create();
  Logger.log('1분 트리거를 걸었습니다. 이제 DM 에 링크를 넣으면 1분 안에 답이 옵니다.');
}

/** 트리거를 끄고 싶을 때 실행합니다. 끄면 5분짜리 실행이 20분 뒤부터 대신 받습니다. */
function removeNewsBotMessagesTrigger() {
  ScriptApp.getProjectTriggers()
    .filter(function (t) { return t.getHandlerFunction() === 'wakeNewsBotMessages'; })
    .forEach(function (t) { ScriptApp.deleteTrigger(t); });
}

/** 지금 걸려 있는 트리거를 확인합니다. */
function checkNewsBotMessagesTrigger() {
  const ts = ScriptApp.getProjectTriggers()
    .filter(function (t) { return t.getHandlerFunction() === 'wakeNewsBotMessages'; });
  Logger.log(ts.length ? 'news-bot-messages 트리거 ' + ts.length + '개 작동 중'
                       : 'news-bot-messages 트리거 없음');
}

function dispatchNewsBotMessages() {
  const token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) {
    throw new Error('스크립트 속성에 GITHUB_TOKEN 이 없습니다.');
  }

  const url = 'https://api.github.com/repos/' + NBM_OWNER + '/' + NBM_REPO +
              '/actions/workflows/' + NBM_WORKFLOW + '/dispatches';

  const res = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    headers: { Authorization: 'Bearer ' + token, Accept: 'application/vnd.github+json' },
    payload: JSON.stringify({ ref: 'main' }),
    muteHttpExceptions: true,
  });

  const code = res.getResponseCode();
  if (code !== 204) {
    Logger.log('dispatch 실패 ' + code + ': ' + res.getContentText().slice(0, 300));
  }
  return code;
}

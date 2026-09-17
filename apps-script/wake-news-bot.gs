/**
 * news-bot — 5분마다 GitHub Actions 깨우기
 *
 * 왜 필요한가
 *   GitHub 의 예약(cron)은 약속을 지키지 않습니다. 2026-09-17 실측으로
 *   5분 간격으로 걸어둔 워크플로가 49시간 동안 15번 돌았습니다(간격 중앙값 4시간).
 *   이래서는 속보가 속보가 아닙니다. 구글 트리거는 구글 서버에서 돌기 때문에
 *   제 시각에 뜨고, PC 가 꺼져 있어도 상관없습니다.
 *
 * 이 파일은 '뉴스 클리핑 트리거' 프로젝트에 덧붙이는 것을 전제로 합니다.
 * 그쪽 코드가 이미 쓰고 있는 이름(OWNER, testNow, setupTrigger …)과 겹치지 않도록
 * 전부 NB_ / NewsBot 을 붙여 두었습니다. GITHUB_TOKEN 은 그 프로젝트 것을 같이 씁니다.
 *
 * 설치
 *   1) 이 내용을 프로젝트에 붙여넣고 저장
 *   2) 함수 목록에서 testNewsBot 실행 → 로그에 성공이 뜨는지 확인
 *   3) 함수 목록에서 setupNewsBotTrigger 실행 → 5분마다 자동
 *
 * 할당량
 *   5분 간격 = 하루 288회, 한 번에 1초도 안 걸립니다.
 *   무료 한도(하루 90분, UrlFetch 2만회) 안에 넉넉히 들어갑니다.
 */

const NB_OWNER = 'dlacksgud11111-tech';
const NB_REPO = 'news-bot';
const NB_WORKFLOW = 'bot.yml';

/** 5분마다 트리거가 부르는 함수입니다. */
function wakeNewsBot() {
  dispatchNewsBot();
}

/** 설치 직후 손으로 한 번 실행해 연결을 확인하는 용도입니다. */
function testNewsBot() {
  const code = dispatchNewsBot();
  if (code === 204) {
    Logger.log('성공 — GitHub Actions 탭에 실행이 떴는지 확인하세요.');
  } else if (code === 404) {
    Logger.log('실패 404 — 토큰에 news-bot 저장소 권한이 없습니다. ' +
               'GITHUB_TOKEN 이 fine-grained 라면 news-bot 도 포함하도록 고치거나, ' +
               'repo + workflow 권한의 classic 토큰을 쓰세요.');
  } else {
    Logger.log('실패 — 응답 코드 ' + code);
  }
}

/** 5분 트리거를 겁니다. 이미 걸려 있으면 지우고 새로 겁니다. */
function setupNewsBotTrigger() {
  removeNewsBotTrigger();
  ScriptApp.newTrigger('wakeNewsBot').timeBased().everyMinutes(5).create();
  Logger.log('5분 트리거를 걸었습니다.');
}

/** 트리거를 끄고 싶을 때 실행합니다. */
function removeNewsBotTrigger() {
  ScriptApp.getProjectTriggers()
    .filter(function (t) { return t.getHandlerFunction() === 'wakeNewsBot'; })
    .forEach(function (t) { ScriptApp.deleteTrigger(t); });
}

/** 지금 걸려 있는 트리거를 확인합니다. */
function checkNewsBotTrigger() {
  const ts = ScriptApp.getProjectTriggers()
    .filter(function (t) { return t.getHandlerFunction() === 'wakeNewsBot'; });
  Logger.log(ts.length ? 'news-bot 트리거 ' + ts.length + '개 작동 중' : 'news-bot 트리거 없음');
}

function dispatchNewsBot() {
  const token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) {
    throw new Error('스크립트 속성에 GITHUB_TOKEN 이 없습니다.');
  }

  const url = 'https://api.github.com/repos/' + NB_OWNER + '/' + NB_REPO +
              '/actions/workflows/' + NB_WORKFLOW + '/dispatches';

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

/**
 * news-bot — 구글 앱스 스크립트 트리거 (5분마다 GitHub Actions 깨우기)
 *
 * 왜 필요한가
 *   GitHub 의 예약(cron)은 약속을 지키지 않습니다.
 *   2026-09-17 실측: bot.yml 을 5분 간격으로 걸어뒀는데 49시간 동안 15번 실행,
 *   실행 간격 중앙값이 4시간이었습니다. 이래서는 속보가 속보가 아닙니다.
 *   구글 트리거는 구글 서버에서 돌기 때문에 제 시각에 뜨고, PC 와 무관합니다.
 *
 * 설치 (News-Clipper 스크립트에 이미 GITHUB_TOKEN 을 넣어두셨다면 그 프로젝트에
 *      이 파일을 추가하는 것이 가장 간단합니다 — 토큰을 다시 넣을 필요가 없습니다)
 *   1) script.google.com 에서 기존 프로젝트 열기
 *   2) 파일 + → 스크립트 → 이 내용 붙여넣기
 *   3) 함수 목록에서 testNow 선택 → 실행 → GitHub Actions 에 실행이 뜨는지 확인
 *   4) 함수 목록에서 setupTrigger 선택 → 실행 → 5분마다 자동
 *
 * 새 프로젝트로 만드신다면 3번 전에
 *   프로젝트 설정 > 스크립트 속성 > 속성 추가 > GITHUB_TOKEN = (깃허브 토큰)
 *
 * 할당량
 *   5분 간격 = 하루 288회. 한 번에 1초도 안 걸리므로 무료 한도(하루 90분,
 *   UrlFetch 2만회) 안에 넉넉히 들어갑니다.
 */

const OWNER = 'dlacksgud11111-tech';
const REPO = 'news-bot';
const WORKFLOW = 'bot.yml';

/** 5분마다 트리거가 부르는 함수입니다. */
function wakeNewsBot() {
  dispatchNewsBot();
}

/** 설치 직후 손으로 한 번 실행해 연결을 확인하는 용도입니다. */
function testNow() {
  const code = dispatchNewsBot();
  Logger.log(code === 204 ? '성공 — GitHub Actions 탭에서 실행을 확인하세요.'
                          : '실패 — 응답 코드 ' + code);
}

/** 5분 트리거를 겁니다. 이미 걸려 있으면 지우고 새로 겁니다. */
function setupTrigger() {
  ScriptApp.getProjectTriggers()
    .filter(function (t) { return t.getHandlerFunction() === 'wakeNewsBot'; })
    .forEach(function (t) { ScriptApp.deleteTrigger(t); });

  ScriptApp.newTrigger('wakeNewsBot').timeBased().everyMinutes(5).create();
  Logger.log('5분 트리거를 걸었습니다.');
}

/** 트리거를 끄고 싶을 때 실행합니다. */
function removeTrigger() {
  ScriptApp.getProjectTriggers()
    .filter(function (t) { return t.getHandlerFunction() === 'wakeNewsBot'; })
    .forEach(function (t) { ScriptApp.deleteTrigger(t); });
  Logger.log('트리거를 껐습니다.');
}

function dispatchNewsBot() {
  const token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) {
    throw new Error('스크립트 속성에 GITHUB_TOKEN 이 없습니다. 프로젝트 설정 > 스크립트 속성에서 추가하세요.');
  }

  const url = 'https://api.github.com/repos/' + OWNER + '/' + REPO +
              '/actions/workflows/' + WORKFLOW + '/dispatches';

  const res = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    headers: { Authorization: 'Bearer ' + token, Accept: 'application/vnd.github+json' },
    payload: JSON.stringify({ ref: 'main' }),
    muteHttpExceptions: true,
  });

  const code = res.getResponseCode();
  if (code !== 204) {
    // 404 는 토큰에 이 저장소 권한이 없을 때도 납니다 (없는 저장소와 구분되지 않음).
    Logger.log('dispatch 실패 ' + code + ': ' + res.getContentText().slice(0, 300));
  }
  return code;
}

import React from 'react';
import fetchMock from 'fetch-mock';
import { render, cleanup, waitFor, fireEvent } from '@testing-library/react';

import {
  getProjectUrl,
  replaceLocation,
  setUrlParam,
} from '../../../ui/helpers/location';
import PlatformConfig from '../../../ui/push-health/PlatformConfig';
import pushHealth from '../mock/push_health';
import fullJob from '../mock/full_job.json';
import bugSuggestions from '../mock/bug_suggestions.json';

const repoName = 'autoland';
const { jobs } = pushHealth;
const testFailure = pushHealth.metrics.tests.details.needInvestigation[2];

beforeEach(() => {
  fetchMock.get('https://treestatus.mozilla-releng.net/trees/autoland', {
    result: {
      message_of_the_day: '',
      reason: '',
      status: 'open',
      tree: 'autoland',
    },
  });
  setUrlParam('repo', repoName);
  fetchMock.get(getProjectUrl('/jobs/285857770/', repoName), fullJob);
  fetchMock.get(getProjectUrl('/jobs/285852303/', repoName), fullJob);
  fetchMock.get(
    'http://foo.com/api/queue/v1/task/fmIhWrXlQVmXCZ4aUQRYvw/runs/0/artifacts',
    { artifacts: [{ name: 'http://baz.com/thing.log' }] },
  );
  fetchMock.get(
    'http://foo.com/api/queue/v1/task/DNRiluCjQOeFdxyubn1kbA/runs/0/artifacts',
    { artifacts: [{ name: 'http://baz.com/thing.log' }] },
  );
  fetchMock.get(
    getProjectUrl('/jobs/303550431/bug_suggestions/', repoName),
    bugSuggestions,
  );
  testFailure.key = 'wazzon';
});

afterEach(() => {
  cleanup();
  fetchMock.reset();
  replaceLocation({});
});

describe('PlatformConfig', () => {
  const testPlatformConfig = (failure, jobs) => (
    <PlatformConfig
      failure={failure}
      jobs={jobs}
      repo="autoland"
      user={{ email: 'foo' }}
      revision="abc"
      currentRepo={{ name: repoName, tc_root_url: 'http://foo.com' }}
      groupedBy="platform"
      notify={() => {}}
      updateParamsAndState={() => {}}
    />
  );

  test('should show the test name', async () => {
    const { getByText } = render(testPlatformConfig(testFailure, jobs));

    expect(
      await waitFor(() =>
        getByText(
          'layout/reftests/high-contrast/backplate-bg-image-006.html == layout/reftests/high-contrast/backplate-bg-image-006-ref.html',
        ),
      ),
    ).toBeInTheDocument();
  });

  test('should not show details by default', async () => {
    const { queryByTestId } = render(testPlatformConfig(testFailure, jobs));

    expect(queryByTestId('log-lines')).toBeNull();
  });

  test('should show bug suggestions when expander clicked', async () => {
    const { getByText } = render(testPlatformConfig(testFailure, jobs));
    const detailsButton = getByText('task');

    fireEvent.click(detailsButton);

    expect(
      await waitFor(() =>
        getByText('There must be some page title', { exact: false }),
      ),
    ).toBeVisible();
  });

  test('should show artifacts when tab clicked', async () => {
    const { getByText } = render(testPlatformConfig(testFailure, jobs));
    const detailsButton = getByText('task');

    fireEvent.click(detailsButton);

    const artifactsTab = await waitFor(() => getByText('Artifacts'));

    fireEvent.click(artifactsTab);

    expect(await waitFor(() => getByText('thing.log'))).toBeVisible();
  });
});

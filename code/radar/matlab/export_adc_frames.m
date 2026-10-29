function frameTable = export_adc_frames(chainRoot, captureFolder, calibrationMat, moduleParamFile, outputDir)
%EXPORT_ADC_FRAMES Export one capture using the installed TI example chain.
% Based on the project's get_adcData.m calibration-only export workflow.
% Requires a local TI mmWave Studio 02.01.01.00 four-chip MATLAB example
% installation, capture configuration, device binaries/indexes and the
% appropriate calibration MAT. TI/JSONLab dependencies retain their original
% notices in that installation; vendor source is not bundled here.
%
% Example, after adding this function's directory to the MATLAB path:
% export_adc_frames(chainRoot, captureFolder, calibrationMat, ...
%                   moduleParamFile, 'new_adc_export');
%
% Output: adcData_frame_####.mat (variable adcData, MATLAB -v7.3),
% frame_sources.csv and generated_parameters.m. No FFT is performed.
% Export numbering is continuous across chunks; frame_sources.csv preserves
% each chunk's original local frame number. Existing output is never replaced.
% This adapter has not been executed against participant recordings here.

narginchk(5, 5);
chainRoot = existingPath(chainRoot);
captureFolder = existingPath(captureFolder);
calibrationMat = existingPath(calibrationMat);
moduleParamFile = existingPath(moduleParamFile);
assert(isfolder(chainRoot) && isfolder(captureFolder), 'Expected chain and capture folders.');
assert(isfile(calibrationMat) && isfile(moduleParamFile), 'Calibration and module parameters must be files.');
assert(~isfolder(outputDir) && ~isfile(outputDir), 'Choose a new output directory.');
assert(isfile(fullfile(chainRoot, 'add_paths.m')), 'TI add_paths.m is missing.');

originalFolder = pwd;
originalEnvironment = getenv('CASCADE_SIGNAL_PROCESSING_CHAIN_MIMO');
restoreState = onCleanup(@() restoreEnvironment(originalFolder, originalEnvironment)); %#ok<NASGU>
[ok, message] = mkdir(outputDir);
assert(ok, message);
outputDir = existingPath(outputDir);
setenv('CASCADE_SIGNAL_PROCESSING_CHAIN_MIMO', chainRoot);
run(fullfile(chainRoot, 'add_paths.m'));

% TI's parameter generator opens header.m relative to its working directory.
cd(fullfile(chainRoot, 'main', 'cascade', 'paramGen'));
parameterFile = fullfile(outputDir, 'generated_parameters.m');
parameter_file_gen_json(captureFolder, calibrationMat, moduleParamFile, parameterFile, 'TDA2');
calibrationObj = calibrationCascade('pfile', parameterFile, 'calibrationfilePath', calibrationMat);
assert(calibrationObj.adcCalibrationOn == 1, 'This export requires the calibration-enabled module configuration.');

chunkIds = getUniqueFileIdx(captureFolder);
assert(~isempty(chunkIds), 'No native device-data chunks found.');
exportNumber = zeros(0, 1);
sourceChunk = cell(0, 1);
sourceFrame = zeros(0, 1);
exportFilename = cell(0, 1);
count = 0;
for chunk = 1:numel(chunkIds)
    names = getBinFileNames_withIdx(captureFolder, chunkIds{chunk});
    calibrationObj.binfilePath = names;
    [validFrames, ~] = getValidNumFrames(fullfile(captureFolder, names.masterIdxFile));
    for localFrame = 1:validFrames
        calibrationObj.frameIdx = localFrame;
        adcData = datapath(calibrationObj); %#ok<NASGU>
        assert(isequal(size(adcData), [80 25 16 12]), 'Unexpected RAD-EMO ADC shape.');
        count = count + 1;
        filename = sprintf('adcData_frame_%04d.mat', count);
        save(fullfile(outputDir, filename), 'adcData', '-v7.3');
        exportNumber(count, 1) = count;
        sourceChunk{count, 1} = chunkIds{chunk};
        sourceFrame(count, 1) = localFrame;
        exportFilename{count, 1} = filename;
    end
end
frameTable = table(exportNumber, sourceChunk, sourceFrame, exportFilename, ...
    'VariableNames', {'export_frame_number', 'source_chunk_id', 'source_frame_number', 'filename'});
writetable(frameTable, fullfile(outputDir, 'frame_sources.csv'));
fprintf('Exported %d ADC frames to the selected output directory.\n', count);
end

function result = existingPath(value)
[ok, info] = fileattrib(char(value));
assert(ok, 'Required input path does not exist.');
result = info.Name;
end

function restoreEnvironment(folder, previousValue)
cd(folder);
setenv('CASCADE_SIGNAL_PROCESSING_CHAIN_MIMO', previousValue);
end
